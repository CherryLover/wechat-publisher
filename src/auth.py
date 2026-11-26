"""
鉴权模块（支持多租户 MCP Token + 管理员 AUTH_TOKEN + 绑定文章的临时预览 Token）
"""

import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request
from dotenv import load_dotenv

load_dotenv()

# 兼容管理员主 token
AUTH_TOKEN = os.getenv("AUTH_TOKEN")

# 临时 token 数据库路径（与 article.py 共用同一库）
_TOKEN_DB_PATH = Path(os.getenv("DATA_DIR", "/app/data")) / "articles.db"

# 临时 token 有效期（小时）
PREVIEW_TOKEN_EXPIRY_HOURS = 8


def _get_db() -> sqlite3.Connection:
    """获取数据库连接并兼容迁移字段"""
    _TOKEN_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_TOKEN_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS preview_tokens (
            token TEXT PRIMARY KEY,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            user_id TEXT DEFAULT 'owner',
            article_id TEXT
        )
    """)
    for stmt in (
        "ALTER TABLE preview_tokens ADD COLUMN user_id TEXT DEFAULT 'owner'",
        "ALTER TABLE preview_tokens ADD COLUMN article_id TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    return conn


def resolve_bearer_user(request: Request) -> Optional[str]:
    """从 Authorization: Bearer <token> 解析所属 user_id（优先查租户专属 mcp_token，兼容全局 AUTH_TOKEN）"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if not token:
            return None

        from . import tenant
        user = tenant.get_user_by_mcp_token(token)
        if user:
            if user.get("status") != "active":
                raise HTTPException(status_code=403, detail="账号已被停用")
            return user["id"]

        if AUTH_TOKEN and secrets.compare_digest(token, AUTH_TOKEN):
            if not tenant.is_user_active(tenant.OWNER_ID):
                raise HTTPException(status_code=403, detail="账号已被停用")
            return tenant.OWNER_ID
        return None

    # 本地开发未设置 AUTH_TOKEN 时兼容免鉴权走 owner
    if not AUTH_TOKEN:
        from . import tenant
        return tenant.OWNER_ID
    return None


def verify_auth_token(request: Request) -> str:
    """校验 Bearer Token 并返回归属 user_id（FastAPI Depends 用）"""
    uid = resolve_bearer_user(request)
    if not uid:
        raise HTTPException(status_code=401, detail="未授权访问")
    return uid


def get_preview_token_info(token: Optional[str]) -> Optional[dict]:
    """读取并校验未过期的临时预览 token 元信息"""
    if not token:
        return None
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT token, expires_at, COALESCE(user_id, 'owner'), article_id FROM preview_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        expires_at = datetime.fromisoformat(row[1])
        if datetime.now() >= expires_at:
            return None
        return {
            "token": row[0],
            "expires_at": row[1],
            "user_id": row[2] or "owner",
            "article_id": row[3],
        }
    finally:
        conn.close()


def verify_preview_token(token: Optional[str], article_id: Optional[str] = None) -> bool:
    """校验临时预览 token；若 token 绑定了 article_id 则要求一致"""
    if not token:
        return not AUTH_TOKEN
    info = get_preview_token_info(token)
    if not info:
        return False
    if article_id and info.get("article_id") and info["article_id"] != article_id:
        return False
    return True


def verify_bearer_or_preview_token(
    request: Request,
    token: Optional[str],
    article_id: Optional[str] = None,
) -> Optional[str]:
    """校验 Bearer Token 或临时预览 Token，任一通过返回关联 user_id（或 None 表示本地无认证开发模式）"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        uid = resolve_bearer_user(request)
        if uid:
            return uid
        raise HTTPException(status_code=401, detail="未授权访问")

    if token:
        info = get_preview_token_info(token)
        if info and (not article_id or not info.get("article_id") or info["article_id"] == article_id):
            return info["user_id"]
        raise HTTPException(status_code=401, detail="未授权访问")

    if not AUTH_TOKEN:
        from . import tenant
        return tenant.OWNER_ID

    raise HTTPException(status_code=401, detail="未授权访问")


def create_preview_token(user_id: str = "owner", article_id: Optional[str] = None) -> dict:
    """生成临时预览 token，可绑定归属 user_id 与 article_id"""
    cleanup_expired_tokens()

    token = secrets.token_hex(24)
    now = datetime.now()
    expires_at = now + timedelta(hours=PREVIEW_TOKEN_EXPIRY_HOURS)
    uid = user_id or "owner"

    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO preview_tokens (token, expires_at, created_at, user_id, article_id) VALUES (?, ?, ?, ?, ?)",
            (token, expires_at.isoformat(), now.isoformat(), uid, article_id),
        )
        conn.commit()
    finally:
        conn.close()

    return {"token": token, "expires_at": expires_at.isoformat()}


def cleanup_expired_tokens():
    """清理过期的临时 token"""
    conn = _get_db()
    try:
        conn.execute(
            "DELETE FROM preview_tokens WHERE expires_at <= ?",
            (datetime.now().isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()
