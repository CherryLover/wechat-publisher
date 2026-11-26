"""
Bark 推送通知（按租户配置读取）
"""

import logging
from typing import Optional

import httpx

from . import tenant

logger = logging.getLogger(__name__)


async def send(
    title: str,
    body: str,
    level: str = "active",
    user_id: str = "owner",
    bark_override: Optional[dict] = None,
) -> dict:
    """发一条 Bark 通知；传 bark_override 时用于保存前即时测试"""
    cfg = tenant.get_tenant_config(user_id)
    notify = cfg.get("notify") or {}
    if bark_override is None and not notify.get("enabled"):
        return {"ok": False, "reason": "notify_disabled"}

    saved_bark = notify.get("bark") or {}
    ov = bark_override or {}
    key = (ov.get("key") or saved_bark.get("key") or "").strip()
    if not key:
        return {"ok": False, "reason": "no_device_key"}

    server = (ov.get("server") or saved_bark.get("server") or "https://api.day.app").strip().rstrip("/")
    group = (ov.get("group") or saved_bark.get("group") or "公众号发布").strip()

    payload = {
        "title": title[:120],
        "body": body[:400],
        "group": group,
        "level": level,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{server}/{key}", json=payload)
        ok = resp.status_code == 200
        if not ok:
            logger.warning("Bark 推送失败 %s: %s", resp.status_code, resp.text[:200])
        return {"ok": ok, "status": resp.status_code}
    except Exception as e:
        logger.warning("Bark 推送异常: %s", e)
        return {"ok": False, "reason": str(e)}


async def notify_event(
    event: str,
    title: str,
    body: str,
    level: str = "active",
    user_id: str = "owner",
) -> None:
    """按事件开关决定是否推送（event: start / success / error）"""
    cfg = tenant.get_tenant_config(user_id)
    events = ((cfg.get("notify") or {}).get("events")) or {}
    if not events.get(event, False):
        return
    await send(title, body, level, user_id=user_id)
