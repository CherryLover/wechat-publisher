"""多租户账号密码登录与限流集成测试"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import article, auth, config, oidc, password_auth, tenant, web


class TestPasswordAuthUnit(unittest.TestCase):
    def test_hash_and_verify_salted(self):
        h1 = password_auth.hash_password("secret-123456")
        h2 = password_auth.hash_password("secret-123456")
        self.assertNotEqual(h1, h2)
        self.assertNotIn("secret-123456", h1)
        self.assertTrue(password_auth.verify_password("secret-123456", h1))
        self.assertTrue(password_auth.verify_password("secret-123456", h2))
        self.assertFalse(password_auth.verify_password("wrong-password", h1))
        self.assertFalse(password_auth.verify_password("", h1))

    def test_verify_malformed_hashes(self):
        self.assertFalse(password_auth.verify_password("abc123", ""))
        self.assertFalse(password_auth.verify_password("abc123", "plain$text"))
        self.assertFalse(password_auth.verify_password("abc123", "md5$260000$aa$bb"))
        self.assertFalse(password_auth.verify_password("abc123", "pbkdf2_sha256$notint$aa$bb"))
        self.assertFalse(password_auth.verify_password("abc123", "pbkdf2_sha256$100$aa$bb"))

    def test_is_enabled_requires_flag_and_hash(self):
        self.assertFalse(password_auth.is_enabled({"password_login": {"enabled": False, "password_hash": ""}}))
        self.assertFalse(password_auth.is_enabled({"password_login": {"enabled": True, "password_hash": ""}}))
        self.assertFalse(password_auth.is_enabled({"password_login": {"enabled": False, "password_hash": "x"}}))
        self.assertTrue(password_auth.is_enabled({"password_login": {"enabled": True, "password_hash": "x"}}))

    def test_rate_limiter_blocks_and_expires_and_resets(self):
        now = [100.0]
        limiter = password_auth.LoginRateLimiter(max_attempts=3, window_seconds=60, now_fn=lambda: now[0])
        ip = "1.2.3.4"
        self.assertFalse(limiter.is_blocked(ip))
        limiter.record_failure(ip)
        limiter.record_failure(ip)
        self.assertFalse(limiter.is_blocked(ip))
        limiter.record_failure(ip)
        self.assertTrue(limiter.is_blocked(ip))

        now[0] += 61
        self.assertFalse(limiter.is_blocked(ip))

        for _ in range(3):
            limiter.record_failure(ip)
        self.assertTrue(limiter.is_blocked(ip))
        limiter.reset(ip)
        self.assertFalse(limiter.is_blocked(ip))


class TestSessionAuthMethod(unittest.TestCase):
    def test_session_preserves_auth_method_and_defaults_legacy_to_oidc(self):
        with patch.object(oidc, "SESSION_SECRET", "test-secret-key"):
            oidc_cookie = oidc.create_session({"id": "owner", "sub": "u1", "name": "Alice", "role": "admin", "auth_method": "oidc"})
            pw_cookie = oidc.create_session({"id": "u_1", "sub": "password", "name": "Bob", "role": "user", "auth_method": "password"})
            s1 = oidc.read_session(oidc_cookie)
            s2 = oidc.read_session(pw_cookie)
            self.assertEqual(s1["auth_method"], "oidc")
            self.assertEqual(s1["uid"], "owner")
            self.assertEqual(s2["auth_method"], "password")
            self.assertEqual(s2["uid"], "u_1")

            # 模拟旧版无 uid/auth_method 的签名 cookie
            legacy_payload = oidc._b64(json.dumps({"sub": "u0", "name": "Old", "exp": 9999999999}).encode())
            legacy_cookie = f"{legacy_payload}.{oidc._sign(legacy_payload.encode())}"
            s_legacy = oidc.read_session(legacy_cookie)
            self.assertEqual(s_legacy["auth_method"], "oidc")
            self.assertEqual(s_legacy["uid"], "owner")
            self.assertEqual(s_legacy["role"], "admin")


class TestPasswordLoginRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_dir = Path(self._tmp.name)
        self.db_path = tmp_dir / "articles.db"
        self.cfg_path = tmp_dir / "config.json"
        self.patches = [
            patch.object(config, "CONFIG_PATH", self.cfg_path),
            patch.object(tenant, "DB_PATH", self.db_path),
            patch.object(article, "DB_PATH", self.db_path),
            patch.object(auth, "_TOKEN_DB_PATH", self.db_path),
            patch.object(oidc, "SESSION_SECRET", "unit-test-secret"),
            patch.object(oidc, "ISSUER", "https://id.example.com"),
            patch.object(oidc, "CLIENT_ID", "cid"),
            patch.object(oidc, "CLIENT_SECRET", "csec"),
            patch.object(oidc, "REDIRECT_URI", "https://pub.example.com/auth/callback"),
            patch.object(oidc, "build_authorize_url", new=AsyncMock(return_value="https://id.example.com/authorize?x=1")),
            patch.object(password_auth, "rate_limiter", password_auth.LoginRateLimiter(max_attempts=3, window_seconds=300)),
            patch.object(tenant, "needs_onboarding", return_value=False),
        ]
        for p in self.patches:
            p.start()

        app = FastAPI()
        app.include_router(web.router)
        self.client = TestClient(app)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def _admin_oidc_cookie(self) -> dict[str, str]:
        tenant.bootstrap_owner()
        val = oidc.create_session({"id": "owner", "username": "admin", "role": "admin", "sub": "u1", "name": "Owner", "auth_method": "oidc"})
        return {oidc.SESSION_COOKIE: val}

    def test_admin_password_setup_and_login_rate_limit(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("使用 Passkey 登录", resp.text)
        self.assertNotIn("管理员默认 admin", resp.text)
        self.assertNotIn("管理员快捷通道", resp.text)
        self.assertIn("/login/password", resp.text)

        # 管理员尚未设置密码时登录失败
        post = self.client.post("/login/password", data={"username": "admin", "password": "any-password"}, follow_redirects=False)
        self.assertEqual(post.status_code, 303)
        self.assertIn("pw_err=invalid", post.headers["location"])

        # 管理员通过 Pocket ID 登录后在设置页设置太短的密码 -> 提示 short
        cookies = self._admin_oidc_cookie()
        r_short = self.client.post(
            "/settings",
            data={"theme": "default", "new_password": "123"},
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(r_short.headers["location"], "/settings?saved=1&pw_err=short")

        # 设置合规密码 -> 成功，数据库存哈希不含明文
        r_ok = self.client.post(
            "/settings",
            data={"theme": "default", "new_password": "MyFixedPass99"},
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(r_ok.headers["location"], "/settings?saved=1")
        owner = tenant.get_user_by_id("owner")
        self.assertNotIn("MyFixedPass99", owner["password_hash"])

        # 连续 3 次错误密码触发限流
        for _ in range(3):
            bad = self.client.post(
                "/login/password",
                data={"username": "admin", "password": "wrong"},
                headers={"X-Forwarded-For": "10.0.0.1"},
                follow_redirects=False,
            )
            self.assertIn("pw_err=invalid", bad.headers["location"])
        blocked = self.client.post(
            "/login/password",
            data={"username": "admin", "password": "MyFixedPass99"},
            headers={"X-Forwarded-For": "10.0.0.1"},
            follow_redirects=False,
        )
        self.assertEqual(blocked.headers["location"], "/login?pw_err=rate_limited")

        # 换 IP 正确密码登录成功
        ok = self.client.post(
            "/login/password",
            data={"username": "admin", "password": "MyFixedPass99"},
            headers={"X-Forwarded-For": "10.0.0.2"},
            follow_redirects=False,
        )
        self.assertEqual(ok.status_code, 303)
        self.assertEqual(ok.headers["location"], "/")
        session_cookie = ok.cookies.get(oidc.SESSION_COOKIE)
        self.assertIsNotNone(session_cookie)
        self.assertEqual(oidc.read_session(session_cookie)["auth_method"], "password")

    def test_login_works_when_oidc_temporarily_down(self):
        with patch.object(oidc, "build_authorize_url", new=AsyncMock(side_effect=RuntimeError("OIDC down"))):
            resp = self.client.get("/login")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Passkey 登录暂时不可用", resp.text)
            self.assertIn("/login/password", resp.text)


if __name__ == "__main__":
    unittest.main()
