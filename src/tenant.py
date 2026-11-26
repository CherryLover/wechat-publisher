"""
多租户管理模块（SQLite users 表 + 租户配置 + 种子派生隔离链接）

- 管理员主账号 id="owner"（username="admin"），启动时幂等迁移既有配置与环境变量
- 每个租户配置好微信 AppID + AppSecret 后自动生成随机 seed，派生专属 mcp_token / webhook_token
- 敏感密钥落库前通过 crypto.encrypt_secret 加密
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import config, crypto, password_auth
from .wechat import WechatAPI

logger = logging.getLogger(__name__)

OWNER_ID = "owner"
DB_PATH = Path(os.getenv("DATA_DIR", "/app/data")) / "articles.db"

# 当前请求/MCP 任务的租户上下文
current_tenant_id: ContextVar[str] = ContextVar("current_tenant_id", default=OWNER_ID)

_USERNAME_RE = re.compile(r"^[a-z0-9._@-]{3,64}$")

TENANT_DEFAULTS: dict[str, Any] = {
    "theme": "default",
    "wechat": {
        "appid": "",
        "appsecret": "",
        "author": "",
        "verified": False,
    },
    "notion": {
        "token": "",
        "db_id": "",
        "webhook_secret": "",
    },
    "notify": {
        "enabled": False,
        "bark": {
            "key": "",
            "server": "https://api.day.app",
            "group": "公众号发布",
        },
        "events": {"start": True, "success": True, "error": True},
    },
    # 用户级 AI 配图偏好（API Key / 模型等由平台统一提供，在 config.json 管理）
    "ai_images": {
        "enabled": False,
        "style_mode": "auto",
        "style": "",
        "write_to_notion": True,
        "skip_if_has_images": False,
        "skip_min_images": 2,
    },
}

_wechat_pool: dict[str, tuple[str, WechatAPI]] = {}
_pool_lock = threading.Lock()
_boot_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_users_table() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'active',
                oidc_sub TEXT UNIQUE,
                seed TEXT NOT NULL DEFAULT '',
                mcp_token TEXT UNIQUE,
                webhook_token TEXT UNIQUE,
                config_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _encrypt_tenant_cfg(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    wx = out.get("wechat") or {}
    if wx.get("appsecret"):
        wx["appsecret"] = crypto.encrypt_secret(str(wx["appsecret"]))
    nt = out.get("notion") or {}
    if nt.get("token"):
        nt["token"] = crypto.encrypt_secret(str(nt["token"]))
    if nt.get("webhook_secret"):
        nt["webhook_secret"] = crypto.encrypt_secret(str(nt["webhook_secret"]))
    bark = (out.get("notify") or {}).get("bark") or {}
    if bark.get("key"):
        bark["key"] = crypto.encrypt_secret(str(bark["key"]))
    return out


def _decrypt_tenant_cfg(cfg: dict) -> dict:
    out = _deep_merge(json.loads(json.dumps(TENANT_DEFAULTS)), cfg)
    wx = out.get("wechat") or {}
    if wx.get("appsecret"):
        wx["appsecret"] = crypto.decrypt_secret(str(wx["appsecret"]))
    nt = out.get("notion") or {}
    if nt.get("token"):
        nt["token"] = crypto.decrypt_secret(str(nt["token"]))
    if nt.get("webhook_secret"):
        nt["webhook_secret"] = crypto.decrypt_secret(str(nt["webhook_secret"]))
    bark = (out.get("notify") or {}).get("bark") or {}
    if bark.get("key"):
        bark["key"] = crypto.decrypt_secret(str(bark["key"]))
    return out


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    raw_cfg = {}
    try:
        raw_cfg = json.loads(row["config_json"] or "{}")
    except Exception:
        raw_cfg = {}
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "password_hash": row["password_hash"] or "",
        "role": row["role"] or "user",
        "status": row["status"] or "active",
        "oidc_sub": row["oidc_sub"],
        "seed": row["seed"] or "",
        "mcp_token": row["mcp_token"] or "",
        "webhook_token": row["webhook_token"] or "",
        "config": _decrypt_tenant_cfg(raw_cfg),
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "last_login_at": row["last_login_at"] or "",
    }


def _short_b64(digest: bytes, length: int = 24) -> str:
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:length]


def derive_tokens(seed: str) -> tuple[str, str]:
    """根据用户种子派生确定性的专属 MCP Token 与 Webhook Token"""
    key = seed.encode("utf-8")
    mcp_tok = "mcp_" + _short_b64(hmac.new(key, b"mcp", hashlib.sha256).digest())
    wh_tok = "wh_" + _short_b64(hmac.new(key, b"webhook", hashlib.sha256).digest())
    return mcp_tok, wh_tok


def _new_seed(appid: str) -> str:
    entropy = f"{secrets.token_hex(24)}:{appid}:{time.time_ns()}"
    return hashlib.sha256(entropy.encode("utf-8")).hexdigest()


def _legacy_webhook_secret() -> str:
    env_sec = os.getenv("NOTION_WEBHOOK_SECRET", "").strip()
    if env_sec:
        return env_sec
    sec_file = Path(os.getenv("DATA_DIR", "/app/data")) / "notion_webhook_secret.txt"
    try:
        if sec_file.exists():
            return sec_file.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def bootstrap_owner() -> dict[str, Any]:
    """幂等初始化管理员 owner 账号，继承单租户时代的环境变量与 config.json"""
    with _boot_lock:
        init_users_table()
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (OWNER_ID,)).fetchone()
            if row:
                raw_json = row["config_json"] or "{}"
                user = _row_to_user(row)
                conn.close()
                # 兼容升级：旧版已有微信配置但尚未记录 verified 字段时自动补齐
                try:
                    raw_wx = (json.loads(raw_json).get("wechat") or {})
                except Exception:
                    raw_wx = {}
                wx_u = (user.get("config") or {}).get("wechat") or {}
                if wx_u.get("appid") and wx_u.get("appsecret") and "verified" not in raw_wx:
                    cfg_patch = json.loads(json.dumps(user["config"]))
                    cfg_patch["wechat"]["verified"] = True
                    save_tenant_config(OWNER_ID, cfg_patch)
                    user = get_user_by_id(OWNER_ID) or user
                # 若已有微信配置但还未生成 seed，则补齐
                return ensure_seed_if_ready(OWNER_ID) or user

            gcfg = config.load_config()
            pw_cfg = gcfg.get("password_login") or {}
            pw_hash = pw_cfg.get("password_hash") if pw_cfg.get("enabled") else ""

            ai_g = gcfg.get("ai_images") or {}
            appid = os.getenv("WX_APPID", "").strip()
            appsecret = os.getenv("WX_APPSECRET", "").strip()
            notion_tok = os.getenv("NOTION_TOKEN", "").strip()
            notion_db_id = (os.getenv("NOTION_DB_ID") or os.getenv("NOTION_DATABASE_ID") or "").strip()
            wh_secret = _legacy_webhook_secret()

            owner_cfg = _deep_merge(
                json.loads(json.dumps(TENANT_DEFAULTS)),
                {
                    "theme": gcfg.get("theme") or "default",
                    "wechat": {
                        "appid": appid,
                        "appsecret": appsecret,
                        "author": "程序杂念",
                        "verified": bool(appid and appsecret),
                    },
                    "notion": {
                        "token": notion_tok,
                        "db_id": notion_db_id,
                        "webhook_secret": wh_secret,
                    },
                    "notify": gcfg.get("notify") or TENANT_DEFAULTS["notify"],
                    "ai_images": {
                        "enabled": bool(ai_g.get("enabled")),
                        "style_mode": ai_g.get("style_mode") or "auto",
                        "style": ai_g.get("style") or "",
                        "write_to_notion": ai_g.get("write_to_notion", True),
                        "skip_if_has_images": bool(ai_g.get("skip_if_has_images")),
                        "skip_min_images": int(ai_g.get("skip_min_images") or 2),
                    },
                },
            )

            seed = ""
            mcp_tok: Optional[str] = None
            wh_tok: Optional[str] = None
            if appid and appsecret:
                seed = _new_seed(appid)
                mcp_tok, wh_tok = derive_tokens(seed)

            now = datetime.now().isoformat()
            enc_cfg_json = json.dumps(_encrypt_tenant_cfg(owner_cfg), ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO users (
                    id, username, display_name, password_hash, role, status,
                    oidc_sub, seed, mcp_token, webhook_token, config_json,
                    created_at, updated_at, last_login_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    OWNER_ID,
                    "admin",
                    "管理员",
                    pw_hash or "",
                    "admin",
                    "active",
                    None,
                    seed,
                    mcp_tok,
                    wh_tok,
                    enc_cfg_json,
                    now,
                    now,
                    None,
                ),
            )
            conn.commit()
            row2 = conn.execute("SELECT * FROM users WHERE id = ?", (OWNER_ID,)).fetchone()
            return _row_to_user(row2)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def get_user_by_id(user_id: str) -> Optional[dict[str, Any]]:
    init_users_table()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not row and user_id == OWNER_ID:
        return bootstrap_owner()
    return _row_to_user(row) if row else None


def get_user_by_mcp_token(token: str) -> Optional[dict[str, Any]]:
    if not token:
        return None
    init_users_table()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE mcp_token = ?", (token.strip(),)).fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def get_user_by_webhook_token(token: str) -> Optional[dict[str, Any]]:
    if not token:
        return None
    init_users_table()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE webhook_token = ?", (token.strip(),)).fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def is_user_active(user_id: str) -> bool:
    u = get_user_by_id(user_id)
    return bool(u and u.get("status") == "active")


def register_user(username: str, password: str, display_name: str = "") -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """注册普通用户，返回 (user, error_code)"""
    init_users_table()
    uname = (username or "").strip().lower()
    if not _USERNAME_RE.match(uname):
        return None, "invalid_username"
    if len(password or "") < password_auth.MIN_PASSWORD_LENGTH:
        return None, "short_password"

    raw_dname = (display_name or "").strip()[:32]
    dname = raw_dname or uname
    uid = "u_" + secrets.token_hex(6)
    pw_hash = password_auth.hash_password(password)
    now = datetime.now().isoformat()
    init_cfg = json.loads(json.dumps(TENANT_DEFAULTS))
    if raw_dname:
        init_cfg["wechat"]["author"] = raw_dname
    cfg_json = json.dumps(_encrypt_tenant_cfg(init_cfg), ensure_ascii=False)

    conn = _connect()
    try:
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (uname,)).fetchone()
        if exists:
            return None, "username_taken"
        conn.execute(
            """
            INSERT INTO users (
                id, username, display_name, password_hash, role, status,
                oidc_sub, seed, mcp_token, webhook_token, config_json,
                created_at, updated_at, last_login_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (uid, uname, dname, pw_hash, "user", "active", None, "", None, None, cfg_json, now, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        return (_row_to_user(row) if row else None), None
    except sqlite3.IntegrityError:
        return None, "username_taken"
    finally:
        conn.close()


def authenticate_user(username: str, password: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """校验账号密码，返回 (user, error_code)"""
    bootstrap_owner()
    uname = (username or "").strip().lower() or "admin"
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (uname,)).fetchone()
        if not row:
            return None, "invalid"
        user = _row_to_user(row)
        if not user["password_hash"] or not password_auth.verify_password(password or "", user["password_hash"]):
            return None, "invalid"
        if user["status"] != "active":
            return None, "disabled"
        now = datetime.now().isoformat()
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user["id"]))
        conn.commit()
        user["last_login_at"] = now
        return user, None
    finally:
        conn.close()


def update_user_password(user_id: str, new_password: str) -> tuple[bool, Optional[str]]:
    if len(new_password or "") < password_auth.MIN_PASSWORD_LENGTH:
        return False, "short"
    u = get_user_by_id(user_id)
    if not u:
        return False, "not_found"
    pw_hash = password_auth.hash_password(new_password)
    now = datetime.now().isoformat()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (pw_hash, now, user_id),
        )
        conn.commit()
        return True, None
    finally:
        conn.close()


def bind_or_get_oidc_user(oidc_user: dict) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Pocket ID 登录绑定：优先匹配 oidc_sub，否则绑定到管理员 owner 账号"""
    bootstrap_owner()
    sub = (oidc_user.get("sub") or "").strip()
    display = oidc_user.get("name") or oidc_user.get("preferred_username") or oidc_user.get("email") or "管理员"

    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE oidc_sub = ?", (sub,)).fetchone() if sub else None
        if not row:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (OWNER_ID,)).fetchone()
            if not row:
                return None, "not_found"
            if sub and not row["oidc_sub"]:
                conn.execute(
                    "UPDATE users SET oidc_sub = ?, display_name = ?, updated_at = ? WHERE id = ?",
                    (sub, display, datetime.now().isoformat(), OWNER_ID),
                )
        user = _row_to_user(conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone())
        if user["status"] != "active":
            return None, "disabled"
        now = datetime.now().isoformat()
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user["id"]))
        conn.commit()
        user["last_login_at"] = now
        return user, None
    finally:
        conn.close()


def ensure_seed_if_ready(user_id: str) -> Optional[dict[str, Any]]:
    """当用户配齐微信 AppID + AppSecret 时自动生成随机 seed 并派生专属链接"""
    u = get_user_by_id(user_id)
    if not u:
        return None
    wx = (u.get("config") or {}).get("wechat") or {}
    appid = (wx.get("appid") or "").strip()
    appsecret = (wx.get("appsecret") or "").strip()
    if not (appid and appsecret):
        return u
    if u.get("seed") and u.get("mcp_token") and u.get("webhook_token"):
        return u

    seed = _new_seed(appid)
    mcp_tok, wh_tok = derive_tokens(seed)
    now = datetime.now().isoformat()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE users SET seed = ?, mcp_token = ?, webhook_token = ?, updated_at = ? WHERE id = ?",
            (seed, mcp_tok, wh_tok, now, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_user_by_id(user_id)


def regenerate_seed(user_id: str) -> Optional[dict[str, Any]]:
    """重新生成用户种子与专属 MCP/Webhook Token，旧链接立即失效"""
    u = get_user_by_id(user_id)
    if not u:
        return None
    wx = (u.get("config") or {}).get("wechat") or {}
    appid = (wx.get("appid") or "").strip()
    appsecret = (wx.get("appsecret") or "").strip()
    if not (appid and appsecret):
        return u

    seed = _new_seed(appid)
    mcp_tok, wh_tok = derive_tokens(seed)
    now = datetime.now().isoformat()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE users SET seed = ?, mcp_token = ?, webhook_token = ?, updated_at = ? WHERE id = ?",
            (seed, mcp_tok, wh_tok, now, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_user_by_id(user_id)


def get_tenant_config(user_id: str) -> dict[str, Any]:
    u = get_user_by_id(user_id)
    if not u:
        return json.loads(json.dumps(TENANT_DEFAULTS))
    return u["config"]


def is_wechat_configured(user_id: str) -> bool:
    """判断租户是否已完成微信公众号配置且通过连通性校验"""
    wx = (get_tenant_config(user_id).get("wechat") or {})
    return bool(
        (wx.get("appid") or "").strip()
        and (wx.get("appsecret") or "").strip()
        and wx.get("verified")
    )


def needs_onboarding(user_id: str) -> bool:
    """必填项未配置完成时每次登录/访问核心页都需要进入引导配置"""
    return not is_wechat_configured(user_id)


def save_tenant_config(user_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """保存租户配置（加密敏感字段，并在微信密钥配齐时自动生成专属种子）"""
    if not get_user_by_id(user_id):
        raise ValueError(f"用户不存在: {user_id}")
    merged = _deep_merge(json.loads(json.dumps(TENANT_DEFAULTS)), cfg)
    enc_json = json.dumps(_encrypt_tenant_cfg(merged), ensure_ascii=False)
    now = datetime.now().isoformat()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE users SET config_json = ?, updated_at = ? WHERE id = ?",
            (enc_json, now, user_id),
        )
        conn.commit()
    finally:
        conn.close()
    ensure_seed_if_ready(user_id)
    return get_tenant_config(user_id)


def get_effective_ai_config(user_id: str) -> dict[str, Any]:
    """合并平台统一 AI 引擎配置（API Key/模型）+ 租户偏好开关"""
    platform_ai = (config.load_config().get("ai_images") or {})
    tenant_ai = (get_tenant_config(user_id).get("ai_images") or {})
    return {
        "enabled": bool(tenant_ai.get("enabled")),
        "base_url": (platform_ai.get("base_url") or "").strip(),
        "api_key": (platform_ai.get("api_key") or "").strip(),
        "text_model": (platform_ai.get("text_model") or "").strip(),
        "image_model": (platform_ai.get("image_model") or "").strip(),
        "image_api": platform_ai.get("image_api") or "auto",
        "max_width": int(platform_ai.get("max_width") or 1200),
        "cover_size": platform_ai.get("cover_size") or "1536x1024",
        "illustration_size": platform_ai.get("illustration_size") or "1536x1024",
        "style_mode": tenant_ai.get("style_mode") or "auto",
        "style": tenant_ai.get("style") or "",
        "write_to_notion": tenant_ai.get("write_to_notion", True),
        "skip_if_has_images": bool(tenant_ai.get("skip_if_has_images")),
        "skip_min_images": max(1, int(tenant_ai.get("skip_min_images") or 2)),
    }


def get_wechat_api(user_id: str) -> Optional[WechatAPI]:
    """按租户获取或复用 WechatAPI 实例；密钥变更时自动重建"""
    cfg = get_tenant_config(user_id)
    wx = cfg.get("wechat") or {}
    appid = (wx.get("appid") or "").strip()
    appsecret = (wx.get("appsecret") or "").strip()
    if not (appid and appsecret):
        return None
    digest = hashlib.sha256(f"{appid}:{appsecret}".encode("utf-8")).hexdigest()
    with _pool_lock:
        cached = _wechat_pool.get(user_id)
        if cached and cached[0] == digest:
            return cached[1]
        inst = WechatAPI(appid, appsecret)
        _wechat_pool[user_id] = (digest, inst)
        return inst


def set_user_status(user_id: str, status: str) -> tuple[bool, Optional[str]]:
    if user_id == OWNER_ID:
        return False, "不能停用主管理员账号"
    if status not in ("active", "disabled"):
        return False, "无效状态"
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
            (status, datetime.now().isoformat(), user_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return False, "用户不存在"
        return True, None
    finally:
        conn.close()


def list_users_with_stats() -> list[dict[str, Any]]:
    """管理员后台用户清单及各租户统计"""
    bootstrap_owner()
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        art_counts: dict[str, int] = {}
        try:
            for uid, cnt in conn.execute(
                "SELECT COALESCE(user_id, 'owner'), COUNT(*) FROM articles GROUP BY COALESCE(user_id, 'owner')"
            ).fetchall():
                art_counts[uid] = int(cnt or 0)
        except sqlite3.OperationalError:
            pass

        usage_counts: dict[str, tuple[int, int]] = {}
        try:
            for uid, calls, imgs in conn.execute(
                "SELECT COALESCE(user_id, 'owner'), COUNT(*), COALESCE(SUM(images),0) FROM api_usage GROUP BY COALESCE(user_id, 'owner')"
            ).fetchall():
                usage_counts[uid] = (int(calls or 0), int(imgs or 0))
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()

    out = []
    for r in rows:
        u = _row_to_user(r)
        cfg = u["config"]
        wx = cfg.get("wechat") or {}
        nt = cfg.get("notion") or {}
        calls, imgs = usage_counts.get(u["id"], (0, 0))
        out.append({
            "id": u["id"],
            "username": u["username"],
            "display_name": u["display_name"],
            "role": u["role"],
            "status": u["status"],
            "wechat_ready": bool(wx.get("appid") and wx.get("appsecret") and wx.get("verified")),
            "notion_ready": bool(nt.get("token") and nt.get("db_id")),
            "seed_ready": bool(u["mcp_token"] and u["webhook_token"]),
            "articles": art_counts.get(u["id"], 0),
            "ai_calls": calls,
            "ai_images": imgs,
            "created_at": (u["created_at"] or "")[:16].replace("T", " "),
            "last_login_at": (u["last_login_at"] or "")[:16].replace("T", " ") or "—",
        })
    return out
