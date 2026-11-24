"""
固定密码登录模块

- 默认不开启，由 Pocket ID 登录后在设置页手动开启并设置密码
- 密码使用 PBKDF2-HMAC-SHA256 + 随机盐存储（不落明文），零第三方依赖
- 按客户端 IP 做简单的内存防爆破限流
"""

import hashlib
import hmac
import secrets
import threading
import time
from typing import Callable, Optional

from . import config

PBKDF2_ALGO = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 260_000
MIN_PASSWORD_LENGTH = 6


def hash_password(password: str) -> str:
    """生成加盐 PBKDF2-HMAC-SHA256 哈希串"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ALGO}${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """常量时间校验密码与哈希是否匹配"""
    if not password or not stored_hash:
        return False
    parts = stored_hash.split("$")
    if len(parts) != 4:
        return False
    algo, iter_str, salt_hex, dk_hex = parts
    if algo != PBKDF2_ALGO:
        return False
    try:
        iterations = int(iter_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except ValueError:
        return False
    if iterations < 10_000:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def is_enabled(cfg: Optional[dict] = None) -> bool:
    """密码登录是否已开启且已配置有效密码哈希"""
    data = cfg if cfg is not None else config.load_config()
    pw = data.get("password_login") or {}
    return bool(pw.get("enabled") and pw.get("password_hash"))


def has_password(cfg: Optional[dict] = None) -> bool:
    """是否已设置过密码"""
    data = cfg if cfg is not None else config.load_config()
    pw = data.get("password_login") or {}
    return bool(pw.get("password_hash"))


class LoginRateLimiter:
    """按 IP 限制密码登录失败次数（滑动窗口）"""

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: int = 300,
        now_fn: Callable[[], float] = time.monotonic,
    ):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._now = now_fn
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, ip: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        ts = [t for t in self._failures.get(ip, []) if t > cutoff]
        if ts:
            self._failures[ip] = ts
        else:
            self._failures.pop(ip, None)
        return ts

    def is_blocked(self, ip: str) -> bool:
        with self._lock:
            now = self._now()
            return len(self._prune(ip, now)) >= self.max_attempts

    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = self._now()
            ts = self._prune(ip, now)
            ts.append(now)
            self._failures[ip] = ts

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


rate_limiter = LoginRateLimiter()
