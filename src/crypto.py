"""
轻量对称认证加密（仅使用 Python 标准库）

用于加密落库租户敏感凭证（微信公众号 AppSecret、Notion Token、Webhook Secret、Bark Key、平台 AI API Key）。
格式：enc:v1:<b64_nonce>:<b64_ciphertext>:<b64_mac>
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"


def _master_key_file() -> Path:
    data_dir = os.getenv("DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir) / ".master_key"
    tenant_mod = sys.modules.get("src.tenant")
    if tenant_mod is not None and getattr(tenant_mod, "DB_PATH", None):
        return Path(tenant_mod.DB_PATH).parent / ".master_key"
    return Path("/app/data") / ".master_key"


def _get_master_key() -> bytes:
    """获取服务端主密钥：优先 ENCRYPTION_KEY / SESSION_SECRET，其次持久化 .master_key"""
    raw = (os.getenv("ENCRYPTION_KEY") or os.getenv("SESSION_SECRET") or "").strip()
    if not raw:
        oidc_mod = sys.modules.get("src.oidc")
        if oidc_mod is not None:
            raw = (getattr(oidc_mod, "SESSION_SECRET", "") or "").strip()
    if raw:
        return hashlib.sha256(raw.encode("utf-8")).digest()

    path = _master_key_file()
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return hashlib.sha256(text.encode("utf-8")).digest()

    path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_hex(32)
    path.write_text(generated, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return hashlib.sha256(generated.encode("utf-8")).digest()


def _derive_keys() -> tuple[bytes, bytes]:
    master = _get_master_key()
    enc_key = hmac.new(master, b"wp-enc-v1", hashlib.sha256).digest()
    mac_key = hmac.new(master, b"wp-mac-v1", hashlib.sha256).digest()
    return enc_key, mac_key


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = []
    counter = 0
    total = 0
    while total < length:
        block = hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        blocks.append(block)
        total += len(block)
        counter += 1
    return b"".join(blocks)[:length]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def encrypt_secret(plain: str) -> str:
    """加密敏感字符串；空串保持为空，已是密文则幂等返回"""
    if not plain:
        return ""
    if plain.startswith(PREFIX):
        return plain
    enc_key, mac_key = _derive_keys()
    nonce = secrets.token_bytes(16)
    pt = plain.encode("utf-8")
    stream = _keystream(enc_key, nonce, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, stream))
    mac = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    return f"{PREFIX}{_b64(nonce)}:{_b64(ct)}:{_b64(mac)}"


def decrypt_secret(value: str) -> str:
    """解密敏感字符串；非 enc:v1: 前缀按老明文原样返回，校验失败返回空串"""
    if not value:
        return ""
    if not value.startswith(PREFIX):
        return value
    rest = value[len(PREFIX):]
    parts = rest.split(":")
    if len(parts) != 3:
        logger.warning("密文格式无效，无法解密")
        return ""
    try:
        nonce = _unb64(parts[0])
        ct = _unb64(parts[1])
        mac = _unb64(parts[2])
    except Exception:
        logger.warning("密文 Base64 解析失败")
        return ""

    _, mac_key = _derive_keys()
    expected_mac = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        logger.warning("密文 MAC 校验失败，拒绝解密")
        return ""

    enc_key, _ = _derive_keys()
    stream = _keystream(enc_key, nonce, len(ct))
    pt = bytes(a ^ b for a, b in zip(ct, stream))
    try:
        return pt.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("密文明文解码 UTF-8 失败")
        return ""
