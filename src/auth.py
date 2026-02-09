"""
鉴权模块

- 主 Token (AUTH_TOKEN): 保护 API 和 MCP 端点，Bearer Token 方式
- 临时 Token: 保护网页预览，8 小时有效，SQLite 存储
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

# 主 token，从环境变量读取
AUTH_TOKEN = os.getenv("AUTH_TOKEN")

# 临时 token 数据库路径（与 article.py 共用同一目录）
_TOKEN_DB_PATH = Path("/app/data/articles.db")

# 临时 token 有效期（小时）
PREVIEW_TOKEN_EXPIRY_HOURS = 8


def _get_db() -> sqlite3.Connection:
    """获取数据库连接"""
    _TOKEN_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_TOKEN_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS preview_tokens (
            token TEXT PRIMARY KEY,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    return conn


def verify_auth_token(request: Request):
    """校验主 Bearer Token（FastAPI Depends 用）。

    AUTH_TOKEN 未设置时跳过鉴权（本地开发兼容）。
    """
    if not AUTH_TOKEN:
        return

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if secrets.compare_digest(token, AUTH_TOKEN):
            return

    raise HTTPException(status_code=401, detail="未授权访问")


def verify_preview_token(token: Optional[str]) -> bool:
    """校验临时预览 token，有效返回 True"""
    if not AUTH_TOKEN:
        # 未配置主 token 时跳过鉴权
        return True

    if not token:
        return False

    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT expires_at FROM preview_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return False
        expires_at = datetime.fromisoformat(row[0])
        return datetime.now() < expires_at
    finally:
        conn.close()


def verify_bearer_or_preview_token(request: Request, token: Optional[str]):
    """校验 Bearer Token 或临时预览 Token，任一通过即可。

    用于预览页发起的操作（主题切换、发布），同时兼容 API 调用和网页操作。
    AUTH_TOKEN 未设置时跳过鉴权。
    """
    if not AUTH_TOKEN:
        return

    # 检查 Bearer Token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and secrets.compare_digest(auth_header[7:], AUTH_TOKEN):
        return

    # 检查临时 Token
    if verify_preview_token(token):
        return

    raise HTTPException(status_code=401, detail="未授权访问")


def create_preview_token() -> dict:
    """生成临时预览 token。

    Returns:
        {"token": "xxx", "expires_at": "ISO 时间"}
    """
    # 先清理过期 token
    cleanup_expired_tokens()

    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires_at = now + timedelta(hours=PREVIEW_TOKEN_EXPIRY_HOURS)

    conn = _get_db()
    try:
        conn.execute(
            "INSERT INTO preview_tokens (token, expires_at, created_at) VALUES (?, ?, ?)",
            (token, expires_at.isoformat(), now.isoformat()),
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
