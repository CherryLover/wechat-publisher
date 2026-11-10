"""
Pocket ID（OpenID Connect）登录

流程：/login → 跳 authorize → /auth/callback 换 token → 取 userinfo → 写签名会话 cookie
不依赖第三方库，用 httpx + 标准库实现。
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ISSUER = os.getenv("OIDC_ISSUER", "").rstrip("/")
CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("OIDC_REDIRECT_URI", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_COOKIE = "wp_session"
SESSION_TTL = int(os.getenv("SESSION_TTL", str(7 * 24 * 3600)))

_STATE_TTL = 600
_states: dict[str, dict] = {}
_discovery: Optional[dict] = None

SCOPES = "openid profile email"


def is_configured() -> bool:
    return bool(ISSUER and CLIENT_ID and CLIENT_SECRET and REDIRECT_URI and SESSION_SECRET)


async def discover() -> dict:
    """拉取并缓存 OIDC 元数据"""
    global _discovery
    if _discovery:
        return _discovery
    url = f"{ISSUER}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"OIDC 发现文档获取失败 {resp.status_code}")
    _discovery = resp.json()
    return _discovery


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest())


# ---------- 会话 ----------

def create_session(user: dict) -> str:
    """生成签名会话 cookie 值"""
    data = {
        "sub": user.get("sub"),
        "name": user.get("name") or user.get("preferred_username") or user.get("email") or "用户",
        "email": user.get("email"),
        "picture": user.get("picture"),
        "exp": int(time.time()) + SESSION_TTL,
    }
    body = _b64(json.dumps(data, ensure_ascii=False).encode())
    return f"{body}.{_sign(body.encode())}"


def read_session(value: Optional[str]) -> Optional[dict]:
    """校验并解析会话 cookie"""
    if not value or "." not in value:
        return None
    body, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(_sign(body.encode()), sig):
        return None
    try:
        data = json.loads(_unb64(body))
    except Exception:
        return None
    if int(data.get("exp", 0)) < time.time():
        return None
    return data


# ---------- 授权流程 ----------

def _cleanup_states() -> None:
    now = time.time()
    for k in [k for k, v in _states.items() if v["exp"] < now]:
        _states.pop(k, None)


async def build_authorize_url() -> str:
    """生成授权跳转地址，登记 state 与 PKCE"""
    meta = await discover()
    _cleanup_states()

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    _states[state] = {"verifier": verifier, "exp": time.time() + _STATE_TTL}

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{meta['authorization_endpoint']}?{httpx.QueryParams(params)}"


def pop_state(state: str) -> Optional[str]:
    """取出并删除 state，返回 PKCE verifier"""
    record = _states.pop(state, None)
    if not record or record["exp"] < time.time():
        return None
    return record["verifier"]


async def exchange_code(code: str, verifier: str) -> dict:
    """用授权码换 access_token"""
    meta = await discover()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code_verifier": verifier,
    }
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(meta["token_endpoint"], data=data)
    if resp.status_code != 200:
        raise RuntimeError(f"换取 token 失败 {resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def fetch_userinfo(access_token: str) -> dict:
    """取用户信息"""
    meta = await discover()
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            meta["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"获取用户信息失败 {resp.status_code}")
    return resp.json()


def logout_url() -> str:
    """Pocket ID 的登出地址（有则用）"""
    base = _discovery.get("end_session_endpoint") if _discovery else None
    if not base:
        return "/"
    return f"{base}?post_logout_redirect_uri={REDIRECT_URI.rsplit('/auth/', 1)[0]}/"
