"""
Bark 推送通知

Bark 的接口很简单：POST {server}/{device_key}
body: {"title": ..., "body": ..., "group": ..., "level": ...}
"""

import logging

import httpx

from .config import load_config

logger = logging.getLogger(__name__)


async def send(title: str, body: str, level: str = "active") -> dict:
    """发一条 Bark 通知。未启用或没填 key 时静默跳过"""
    cfg = load_config()
    notify = cfg.get("notify") or {}
    if not notify.get("enabled"):
        return {"ok": False, "reason": "notify_disabled"}

    bark = notify.get("bark") or {}
    key = (bark.get("key") or "").strip()
    if not key:
        return {"ok": False, "reason": "no_device_key"}

    server = (bark.get("server") or "https://api.day.app").strip().rstrip("/")
    group = (bark.get("group") or "公众号发布").strip()

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


async def notify_event(event: str, title: str, body: str, level: str = "active") -> None:
    """按事件开关决定是否推送（event: start / success / error）"""
    cfg = load_config()
    events = ((cfg.get("notify") or {}).get("events")) or {}
    if not events.get(event, False):
        return
    await send(title, body, level)
