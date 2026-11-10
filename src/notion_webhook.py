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
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from .notion_publish import publish_page, TRIGGER_STATUS
from .notion_source import NotionSource

logger = logging.getLogger(__name__)

router = APIRouter()

# 已处理事件 id（防 Notion 重试导致重复发布）
_seen_events: "OrderedDict[str, bool]" = OrderedDict()
_SEEN_MAX = 500

# 正在发布的页面，避免并发重复
_inflight: set[str] = set()
_locks: dict[str, asyncio.Lock] = {}


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
    secret = os.getenv("NOTION_WEBHOOK_SECRET", "")
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
        logger.info("收到 Notion 验证 token，请填入 NOTION_WEBHOOK_SECRET")
        return {"ok": True, "verification_token_hint": "configure NOTION_WEBHOOK_SECRET"}

    event_id = payload.get("id") or f"{payload.get('type')}:{payload.get('timestamp')}"
    event_type = payload.get("type", "")
    entity = payload.get("entity") or {}
    entity_id = entity.get("id")

    logger.info("收到 Webhook: %s entity=%s", event_type, (entity_id or "")[:8])

    if not _remember(event_id):
        return {"ok": True, "skipped": "duplicate"}

    if entity.get("type") != "page" or event_type not in (
        "page.properties_updated",
        "page.created",
        "page.content_updated",
        "page.updated",
    ):
        return {"ok": True, "skipped": "irrelevant"}

    if not entity_id:
        return {"ok": True, "skipped": "no entity id"}

    # 尽快返回 200，发布放后台
    asyncio.create_task(_handle_page_event(entity_id))
    return {"ok": True, "accepted": True}


@router.get("/notion/webhook/status")
async def webhook_status():
    """自检：看配置齐不齐"""
    source = _get_source()
    return {
        "notion_token": bool(os.getenv("NOTION_TOKEN")),
        "webhook_secret_configured": bool(os.getenv("NOTION_WEBHOOK_SECRET")),
        "wechat_configured": _get_wechat() is not None,
        "trigger_status": TRIGGER_STATUS,
        "inflight": list(_inflight),
        "notion_ok": source is not None,
    }
