"""
Notion Webhook 接收端（支持多租户 /notion/webhook/{webhook_token} + 兼容老端点 /notion/webhook）
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections import OrderedDict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from . import tenant
from .notion_publish import TRIGGER_STATUS, publish_page
from .notion_source import NotionSource

logger = logging.getLogger(__name__)

router = APIRouter()

# 已处理事件 id（按 (user_id, event_id) 防 Notion 重试重复发布）
_seen_events: "OrderedDict[tuple[str, str], bool]" = OrderedDict()
_SEEN_MAX = 1000

# 正在发布的页面（按 (user_id, page_id) 隔离）
_inflight: set[tuple[str, str]] = set()
_locks: dict[tuple[str, str], asyncio.Lock] = {}

# 全局发布并发保护，避免多人同时生图压爆容器内存
_publish_sem = asyncio.Semaphore(4)

# 运行期统计（按 user_id 隔离）
_stats_by_user: dict[str, dict] = {}


def _user_stats(user_id: str) -> dict:
    st = _stats_by_user.get(user_id)
    if st is None:
        st = {
            "received": 0,
            "accepted": 0,
            "skipped": 0,
            "invalid_sig": 0,
            "last_event_at": "",
            "last_event_source": "",
            "last_page_id": "",
        }
        _stats_by_user[user_id] = st
    return st


def _resolve_notion_token(user_id: str) -> str:
    nt = (tenant.get_tenant_config(user_id).get("notion") or {})
    tok = (nt.get("token") or "").strip()
    if not tok and user_id == tenant.OWNER_ID:
        tok = os.getenv("NOTION_TOKEN", "").strip()
    return tok


def _resolve_secret(user_id: str) -> tuple[str, str]:
    """返回 (secret, mode: 'configured'|'env'|'none')"""
    nt = (tenant.get_tenant_config(user_id).get("notion") or {})
    sec = (nt.get("webhook_secret") or "").strip()
    if sec:
        return sec, "configured"
    if user_id == tenant.OWNER_ID:
        env_secret = os.getenv("NOTION_WEBHOOK_SECRET", "").strip()
        if env_secret:
            return env_secret, "env"
    return "", "none"


def _save_user_secret(user_id: str, token: str) -> None:
    try:
        cfg = tenant.get_tenant_config(user_id)
        cfg.setdefault("notion", {})["webhook_secret"] = token.strip()
        tenant.save_tenant_config(user_id, cfg)
        logger.info("已保存租户 %s 的 Notion 验证 token，后续请求将自动验签", user_id)
    except Exception as e:
        logger.error("保存验证 token 失败 (%s): %s", user_id, e)


def get_runtime_stats(user_id: str = tenant.OWNER_ID) -> dict:
    """返回指定租户的 Webhook 配置与运行态统计"""
    secret, mode = _resolve_secret(user_id)
    user_inflight = [pid for (uid, pid) in _inflight if uid == user_id]
    user_seen = sum(1 for (uid, _) in _seen_events if uid == user_id)
    return {
        "notion_token": bool(_resolve_notion_token(user_id)),
        "webhook_secret_configured": bool(secret),
        "secret_mode": mode,
        "wechat_configured": tenant.get_wechat_api(user_id) is not None,
        "trigger_status": TRIGGER_STATUS,
        "inflight": user_inflight,
        "inflight_count": len(user_inflight),
        "seen_events": user_seen,
        **_user_stats(user_id),
    }


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """校验 X-Notion-Signature: sha256=<hex>"""
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _remember(user_id: str, event_id: str) -> bool:
    """记录事件 id；已处理过返回 False"""
    key = (user_id, event_id)
    if key in _seen_events:
        return False
    _seen_events[key] = True
    while len(_seen_events) > _SEEN_MAX:
        _seen_events.popitem(last=False)
    return True


def _get_source(user_id: str) -> Optional[NotionSource]:
    token = _resolve_notion_token(user_id)
    if not token:
        return None
    return NotionSource(token)


async def _handle_page_event(user_id: str, page_id: str) -> None:
    """后台任务：检查状态并发布"""
    key = (user_id, page_id)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if key in _inflight:
            logger.info("页面 %s (%s) 正在发布，跳过", page_id[:8], user_id)
            return
        _inflight.add(key)
        try:
            async with _publish_sem:
                if not tenant.is_user_active(user_id):
                    logger.warning("租户 %s 已停用，跳过发布", user_id)
                    return
                source = _get_source(user_id)
                wechat = tenant.get_wechat_api(user_id)
                if not source or not wechat:
                    logger.error("租户 %s Notion 或微信未配置，无法发布", user_id)
                    return
                result = await publish_page(page_id, source, wechat, user_id=user_id)
                logger.info("页面 %s (%s) 发布结果: %s", page_id[:8], user_id, result["message"])
        except Exception as e:
            logger.exception("发布任务异常 %s (%s): %s", page_id[:8], user_id, e)
        finally:
            _inflight.discard(key)


async def _process_webhook(request: Request, user_id: str):
    if not tenant.is_user_active(user_id):
        raise HTTPException(status_code=403, detail="账号已被停用")

    raw = await request.body()
    secret, _ = _resolve_secret(user_id)
    signature = request.headers.get("X-Notion-Signature", "")
    st = _user_stats(user_id)

    if secret:
        if not verify_signature(raw, signature, secret):
            st["invalid_sig"] += 1
            logger.warning("Webhook 验签失败 (%s)", user_id)
            raise HTTPException(status_code=401, detail="invalid signature")
    else:
        logger.warning("租户 %s 未配置 Webhook Secret，已跳过验签", user_id)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    token = payload.get("verification_token")
    if token:
        _save_user_secret(user_id, token)
        return {"ok": True, "verified": True}

    logger.debug("Webhook 原始报文 (%s): %s", user_id, raw[:600].decode("utf-8", "ignore"))

    source_info = payload.get("source") or {}
    data = payload.get("data") or {}
    entity = payload.get("entity") or {}

    page_id, properties = None, None
    if isinstance(data, dict) and data.get("object") == "page":
        page_id, properties = data.get("id"), data.get("properties")
    elif entity.get("type") == "page":
        page_id = entity.get("id")

    event_id = (
        payload.get("id")
        or source_info.get("event_id")
        or f"{payload.get('type')}:{payload.get('timestamp')}"
    )

    event_source = source_info.get("type") or payload.get("type", "?")
    st["received"] += 1
    st["last_event_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    st["last_event_source"] = str(event_source)
    if page_id:
        st["last_page_id"] = page_id

    logger.info("收到事件 user=%s source=%s page=%s", user_id, event_source, (page_id or "")[:8])

    if not _remember(user_id, str(event_id)):
        st["skipped"] += 1
        return {"ok": True, "skipped": "duplicate"}

    if not page_id:
        st["skipped"] += 1
        return {"ok": True, "skipped": "no page id"}

    if properties:
        status_prop = (properties.get("状态") or {}).get("select") or {}
        if status_prop.get("name") != TRIGGER_STATUS:
            st["skipped"] += 1
            return {"ok": True, "skipped": "status=%s" % status_prop.get("name")}

    st["accepted"] += 1
    asyncio.create_task(_handle_page_event(user_id, page_id))
    return {"ok": True, "accepted": True}


@router.post("/notion/webhook")
async def notion_webhook_legacy(request: Request):
    """兼容老版固定 Webhook 端点（映射到管理员 owner）"""
    tenant.bootstrap_owner()
    return await _process_webhook(request, tenant.OWNER_ID)


@router.post("/notion/webhook/{webhook_token}")
async def notion_webhook_tenant(webhook_token: str, request: Request):
    """多租户专属 Notion Webhook 端点"""
    user = tenant.get_user_by_webhook_token(webhook_token)
    if not user:
        raise HTTPException(status_code=404, detail="invalid webhook token")
    return await _process_webhook(request, user["id"])


@router.get("/notion/webhook/status")
async def webhook_status():
    """自检（管理员视图）"""
    tenant.bootstrap_owner()
    return get_runtime_stats(tenant.OWNER_ID)
