"""
Notion Webhook 接收端

Notion 的 Webhook 只能在集成后台配置，代码里创建不了。
这里负责：验签 → 判断事件 → 后台异步触发发布。
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from .notion_publish import publish_page, TRIGGER_STATUS
from .notion_source import NotionSource

logger = logging.getLogger(__name__)

router = APIRouter()

# Notion 创建 Webhook 时会把 verification_token 发过来，落盘后用它验签
SECRET_FILE = Path(os.getenv("DATA_DIR", "/app/data")) / "notion_webhook_secret.txt"

# 已处理事件 id（防 Notion 重试导致重复发布）
_seen_events: "OrderedDict[str, bool]" = OrderedDict()
_SEEN_MAX = 500

# 正在发布的页面，避免并发重复
_inflight: set[str] = set()
_locks: dict[str, asyncio.Lock] = {}


def _get_secret() -> str:
    """优先读环境变量，其次读首次验证时落盘的 token"""
    env_secret = os.getenv("NOTION_WEBHOOK_SECRET", "").strip()
    if env_secret:
        return env_secret
    try:
        if SECRET_FILE.exists():
            return SECRET_FILE.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("读取 webhook 密钥文件失败: %s", e)
    return ""


def _save_secret(token: str) -> None:
    try:
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(token, encoding="utf-8")
        SECRET_FILE.chmod(0o600)
        logger.info("已保存 Notion 验证 token，后续请求将自动验签")
    except Exception as e:
        logger.error("保存验证 token 失败: %s", e)


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """校验 X-Notion-Signature: sha256=<hex>"""
    if not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _remember(event_id: str) -> bool:
    """记录事件 id；已处理过返回 False"""
    if event_id in _seen_events:
        return False
    _seen_events[event_id] = True
    while len(_seen_events) > _SEEN_MAX:
        _seen_events.popitem(last=False)
    return True


def _get_source() -> Optional[NotionSource]:
    token = os.getenv("NOTION_TOKEN")
    if not token:
        return None
    return NotionSource(token)


def _get_wechat():
    from .main import get_wechat_api

    return get_wechat_api()


async def _handle_page_event(page_id: str) -> None:
    """后台任务：检查状态并发布"""
    lock = _locks.setdefault(page_id, asyncio.Lock())
    async with lock:
        if page_id in _inflight:
            logger.info("页面 %s 正在发布，跳过", page_id[:8])
            return
        _inflight.add(page_id)
        try:
            source = _get_source()
            wechat = _get_wechat()
            if not source or not wechat:
                logger.error("Notion 或微信未配置，无法发布")
                return
            result = await publish_page(page_id, source, wechat)
            logger.info("页面 %s 发布结果: %s", page_id[:8], result["message"])
        except Exception as e:
            logger.exception("发布任务异常 %s: %s", page_id[:8], e)
        finally:
            _inflight.discard(page_id)


@router.post("/notion/webhook")
async def notion_webhook(request: Request):
    """接收 Notion 事件"""
    raw = await request.body()
    secret = _get_secret()
    signature = request.headers.get("X-Notion-Signature", "")

    if secret:
        if not verify_signature(raw, signature, secret):
            logger.warning("Webhook 验签失败")
            raise HTTPException(status_code=401, detail="invalid signature")
    else:
        logger.warning("未配置 NOTION_WEBHOOK_SECRET，已跳过验签")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    # 首次配置校验：Notion 会发带 verification_token 的请求
    token = payload.get("verification_token")
    if token:
        _save_secret(token)
        logger.info("收到 Notion 验证 token，已保存，后续自动验签")
        return {"ok": True, "verified": True}

    logger.debug("Webhook 原始报文: %s", raw[:600].decode("utf-8", "ignore"))

    source_info = payload.get("source") or {}
    data = payload.get("data") or {}
    entity = payload.get("entity") or {}

    # 两种来源都支持：
    # 1) Notion 数据库自动化（Automation）：页面对象直接塞在 data 里
    # 2) 集成 Webhook 订阅：页面 id 在 entity 里
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

    logger.info(
        "收到事件 source=%s page=%s",
        source_info.get("type") or payload.get("type", "?"),
        (page_id or "")[:8],
    )

    if not _remember(event_id):
        return {"ok": True, "skipped": "duplicate"}

    if not page_id:
        return {"ok": True, "skipped": "no page id"}

    # 报文里带了属性就先用它判断，省一次 API 调用
    if properties:
        st = (properties.get("状态") or {}).get("select") or {}
        if st.get("name") != TRIGGER_STATUS:
            return {"ok": True, "skipped": "status=%s" % st.get("name")}

    # 尽快返回 200，发布放后台
    asyncio.create_task(_handle_page_event(page_id))
    return {"ok": True, "accepted": True}


@router.get("/notion/webhook/status")
async def webhook_status():
    """自检：看配置齐不齐"""
    source = _get_source()
    return {
        "notion_token": bool(os.getenv("NOTION_TOKEN")),
        "webhook_secret_configured": bool(_get_secret()),
        "webhook_secret_from_file": (not os.getenv("NOTION_WEBHOOK_SECRET", "").strip()) and SECRET_FILE.exists(),
        "wechat_configured": _get_wechat() is not None,
        "trigger_status": TRIGGER_STATUS,
        "inflight": list(_inflight),
        "notion_ok": source is not None,
    }
