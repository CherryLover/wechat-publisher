"""
关联 Notion Database 信息聚合与缓存（按 user_id 隔离）

- 读取关联数据库元信息（标题、属性结构、链接）与原始文章列表
- 聚合基础统计（总数、状态分布、最近编辑时间）
- 内存 + 磁盘 JSON 双层缓存（默认 TTL 5 分钟），支持手动强制刷新与 API 失败降级
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import tenant
from .notion_source import NotionSource

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
CACHE_PATH = DATA_DIR / "notion_db_cache.json"
DEFAULT_TTL = int(os.getenv("NOTION_CACHE_TTL", "300"))

_mem_caches: dict[str, dict[str, Any]] = {}
_fetch_locks: dict[str, asyncio.Lock] = {}


def _get_lock(user_id: str) -> asyncio.Lock:
    lock = _fetch_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _fetch_locks[user_id] = lock
    return lock


def _cache_path_for(user_id: str) -> Path:
    if user_id == tenant.OWNER_ID:
        return CACHE_PATH
    return CACHE_PATH.parent / f"notion_db_cache_{user_id}.json"


def get_database_id(user_id: str = tenant.OWNER_ID) -> str:
    nt = (tenant.get_tenant_config(user_id).get("notion") or {})
    db_id = (nt.get("db_id") or "").strip()
    if db_id:
        return db_id
    if user_id == tenant.OWNER_ID:
        return (os.getenv("NOTION_DB_ID") or os.getenv("NOTION_DATABASE_ID") or "").strip()
    return ""


def normalize_id(raw: Optional[str]) -> str:
    return (raw or "").replace("-", "").lower()


def _extract_plain(rich_list: Optional[list[dict]]) -> str:
    return "".join(x.get("plain_text", "") for x in (rich_list or [])).strip()


def _extract_title(page: dict) -> str:
    props = page.get("properties") or {}
    named = NotionSource.prop_text(page, "标题")
    if named:
        return str(named).strip()
    for p in props.values():
        if isinstance(p, dict) and p.get("type") == "title":
            t = _extract_plain(p.get("title"))
            if t:
                return t
    return "(无标题)"


def _extract_status(page: dict) -> str:
    props = page.get("properties") or {}
    for key in ("状态", "Status", "status"):
        prop = props.get(key)
        if not isinstance(prop, dict):
            continue
        ptype = prop.get("type")
        if ptype == "select":
            name = (prop.get("select") or {}).get("name")
            if name:
                return str(name)
        elif ptype == "status":
            name = (prop.get("status") or {}).get("name")
            if name:
                return str(name)
    return "未设置"


def _fmt_iso(ts: Optional[str]) -> str:
    if not ts:
        return ""
    return ts[:16].replace("T", " ")


def _parse_snapshot(db_meta: dict, pages: list[dict], truncated: bool, db_id: str) -> dict[str, Any]:
    db_title = _extract_plain(db_meta.get("title")) or "Notion 文章库"
    props_raw = db_meta.get("properties") or {}
    properties = [
        {"name": name, "type": (info.get("type") or "unknown")}
        for name, info in props_raw.items()
        if isinstance(info, dict)
    ]

    status_counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    for p in pages:
        status = _extract_status(p)
        status_counts[status] = status_counts.get(status, 0) + 1
        items.append({
            "id": p.get("id") or "",
            "title": _extract_title(p),
            "status": status,
            "url": p.get("url") or "",
            "created_time": _fmt_iso(p.get("created_time")),
            "last_edited_time": _fmt_iso(p.get("last_edited_time")),
            "has_cover": bool(p.get("cover")),
        })

    total = len(items)
    status_breakdown = [
        {
            "name": name,
            "count": cnt,
            "pct": round(cnt * 100 / total, 1) if total else 0.0,
        }
        for name, cnt in sorted(status_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    now_ts = time.time()
    return {
        "fetched_at_ts": now_ts,
        "fetched_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "database": {
            "id": db_meta.get("id") or db_id,
            "title": db_title,
            "url": db_meta.get("url") or "",
            "created_time": _fmt_iso(db_meta.get("created_time")),
            "last_edited_time": _fmt_iso(db_meta.get("last_edited_time")),
            "properties": properties,
        },
        "stats": {
            "total": total,
            "truncated": truncated,
            "status_counts": status_counts,
            "status_breakdown": status_breakdown,
            "latest_edited_time": items[0]["last_edited_time"] if items else "",
        },
        "items": items,
    }


def _load_disk_cache(user_id: str) -> Optional[dict[str, Any]]:
    path = _cache_path_for(user_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "fetched_at_ts" in data and "database" in data:
            return data
    except Exception as e:
        logger.warning("读取 Notion 数据库缓存失败: %s", e)
    return None


def _save_disk_cache(user_id: str, snapshot: dict[str, Any]) -> None:
    path = _cache_path_for(user_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.warning("写入 Notion 数据库缓存失败: %s", e)


def clear_cache(user_id: Optional[str] = None) -> None:
    """清空内存缓存（测试或配置更新时使用）"""
    if user_id is None:
        _mem_caches.clear()
    else:
        _mem_caches.pop(user_id, None)


def _decorate_meta(
    snapshot: dict[str, Any],
    *,
    cache_hit: bool,
    stale: bool = False,
    error: Optional[str] = None,
    ttl: int = DEFAULT_TTL,
) -> dict[str, Any]:
    age = max(0, int(time.time() - float(snapshot.get("fetched_at_ts", 0))))
    expires_in = max(0, ttl - age)
    out = dict(snapshot)
    out["configured"] = True
    out["cache"] = {
        "hit": cache_hit,
        "stale": stale,
        "age_seconds": age,
        "ttl_seconds": ttl,
        "expires_in_seconds": expires_in,
        "fetched_at": snapshot.get("fetched_at") or "",
    }
    out["error"] = error
    return out


def _resolve_notion_creds(user_id: str, token: Optional[str], db_id: Optional[str]) -> tuple[str, str]:
    nt = (tenant.get_tenant_config(user_id).get("notion") or {})
    tok = (token if token is not None else (nt.get("token") or "")).strip()
    did = (db_id if db_id is not None else (nt.get("db_id") or "")).strip()
    if user_id == tenant.OWNER_ID:
        if not tok:
            tok = os.getenv("NOTION_TOKEN", "").strip()
        if not did:
            did = (os.getenv("NOTION_DB_ID") or os.getenv("NOTION_DATABASE_ID") or "").strip()
    return tok, did


async def get_database_snapshot(
    *,
    user_id: str = tenant.OWNER_ID,
    force_refresh: bool = False,
    ttl: Optional[int] = None,
    token: Optional[str] = None,
    db_id: Optional[str] = None,
) -> dict[str, Any]:
    """获取指定租户关联 Notion Database 快照（带内存+磁盘缓存与失败降级）"""
    effective_ttl = DEFAULT_TTL if ttl is None else max(1, int(ttl))
    tok, did = _resolve_notion_creds(user_id, token, db_id)

    if not tok or not did:
        missing = []
        if not tok:
            missing.append("NOTION_TOKEN")
        if not did:
            missing.append("NOTION_DB_ID")
        return {
            "configured": False,
            "error": f"尚未配置 Notion 数据源：{', '.join(missing)}",
            "database": {"id": did, "title": "未关联", "url": "", "properties": []},
            "stats": {"total": 0, "truncated": False, "status_counts": {}, "status_breakdown": [], "latest_edited_time": ""},
            "items": [],
            "cache": {"hit": False, "stale": False, "age_seconds": 0, "ttl_seconds": effective_ttl, "expires_in_seconds": 0, "fetched_at": ""},
        }

    cached = _mem_caches.get(user_id)
    if cached is None:
        cached = _load_disk_cache(user_id)
        if cached is not None:
            _mem_caches[user_id] = cached

    now = time.time()
    if (
        not force_refresh
        and cached is not None
        and normalize_id((cached.get("database") or {}).get("id")) == normalize_id(did)
        and (now - float(cached.get("fetched_at_ts", 0))) < effective_ttl
    ):
        return _decorate_meta(cached, cache_hit=True, ttl=effective_ttl)

    async with _get_lock(user_id):
        cached = _mem_caches.get(user_id)
        now = time.time()
        if (
            not force_refresh
            and cached is not None
            and normalize_id((cached.get("database") or {}).get("id")) == normalize_id(did)
            and (now - float(cached.get("fetched_at_ts", 0))) < effective_ttl
        ):
            return _decorate_meta(cached, cache_hit=True, ttl=effective_ttl)

        try:
            source = NotionSource(tok)
            db_meta = await source.get_database(did)
            pages, truncated = await source.query_database(
                did,
                sorts=[{"timestamp": "last_edited_time", "direction": "descending"}],
            )
            snapshot = _parse_snapshot(db_meta, pages, truncated, did)
            _mem_caches[user_id] = snapshot
            _save_disk_cache(user_id, snapshot)
            return _decorate_meta(snapshot, cache_hit=False, ttl=effective_ttl)
        except Exception as e:
            logger.warning("拉取 Notion Database 失败 (%s): %s", user_id, e)
            if cached is not None and normalize_id((cached.get("database") or {}).get("id")) == normalize_id(did):
                return _decorate_meta(
                    cached,
                    cache_hit=True,
                    stale=True,
                    error=f"实时同步失败，已展示上次缓存：{e}",
                    ttl=effective_ttl,
                )
            return {
                "configured": True,
                "error": f"拉取 Notion Database 失败：{e}",
                "database": {"id": did, "title": "加载失败", "url": "", "properties": []},
                "stats": {"total": 0, "truncated": False, "status_counts": {}, "status_breakdown": [], "latest_edited_time": ""},
                "items": [],
                "cache": {"hit": False, "stale": False, "age_seconds": 0, "ttl_seconds": effective_ttl, "expires_in_seconds": 0, "fetched_at": ""},
            }
