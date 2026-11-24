"""固定密码登录功能的单元测试与 Web 路由集成测试"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import config, oidc, password_auth, web


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

        # 超过窗口后自动解除
        now[0] += 61
        self.assertFalse(limiter.is_blocked(ip))

        # 再次拉满后手动重置
        for _ in range(3):
            limiter.record_failure(ip)
        self.assertTrue(limiter.is_blocked(ip))
        limiter.reset(ip)
        self.assertFalse(limiter.is_blocked(ip))


class TestSessionAuthMethod(unittest.TestCase):
    def test_session_preserves_auth_method_and_defaults_legacy_to_oidc(self):
        with patch.object(oidc, "SESSION_SECRET", "test-secret-key"):
            oidc_cookie = oidc.create_session({"sub": "u1", "name": "Alice", "auth_method": "oidc"})
            pw_cookie = oidc.create_session({"sub": "password", "name": "密码登录", "auth_method": "password"})
            self.assertEqual(oidc.read_session(oidc_cookie)["auth_method"], "oidc")
            self.assertEqual(oidc.read_session(pw_cookie)["auth_method"], "password")

            # 模拟旧版无 auth_method 的签名 cookie
            legacy_payload = oidc._b64(json.dumps({"sub": "u0", "name": "Old", "exp": 9999999999}).encode())
            legacy_cookie = f"{legacy_payload}.{oidc._sign(legacy_payload.encode())}"
            self.assertEqual(oidc.read_session(legacy_cookie)["auth_method"], "oidc")


class TestPasswordLoginRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self._tmp.name) / "config.json"
        self.p_cfg = patch.object(config, "CONFIG_PATH", self.cfg_path)
        self.p_secret = patch.object(oidc, "SESSION_SECRET", "unit-test-secret")
        self.p_issuer = patch.object(oidc, "ISSUER", "https://id.example.com")
        self.p_cid = patch.object(oidc, "CLIENT_ID", "cid")
        self.p_csec = patch.object(oidc, "CLIENT_SECRET", "csec")
        self.p_redir = patch.object(oidc, "REDIRECT_URI", "https://pub.example.com/auth/callback")
        self.p_auth_url = patch.object(
            oidc, "build_authorize_url", new=AsyncMock(return_value="https://id.example.com/authorize?x=1")
        )
        self.p_articles = patch.object(web.article, "list_articles", return_value=[])
        self.p_preview = patch.object(web, "create_preview_token", return_value={"token": "t"})
        self.p_limiter = patch.object(
            password_auth, "rate_limiter", password_auth.LoginRateLimiter(max_attempts=3, window_seconds=300)
        )
        for p in (
            self.p_cfg, self.p_secret, self.p_issuer, self.p_cid, self.p_csec,
            self.p_redir, self.p_auth_url, self.p_articles, self.p_preview, self.p_limiter,
        ):
            p.start()

        app = FastAPI()
        app.include_router(web.router)
        self.client = TestClient(app)

    def tearDown(self):
        for p in (
            self.p_limiter, self.p_preview, self.p_articles, self.p_auth_url,
            self.p_redir, self.p_csec, self.p_cid, self.p_issuer, self.p_secret, self.p_cfg,
        ):
            p.stop()
        self._tmp.cleanup()

    def _oidc_cookie(self) -> dict[str, str]:
        val = oidc.create_session({"sub": "u1", "name": "Owner", "auth_method": "oidc"})
        return {oidc.SESSION_COOKIE: val}

    def test_disabled_by_default(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("使用 Pocket ID 登录", resp.text)
        self.assertNotIn("/login/password", resp.text)

        post = self.client.post("/login/password", data={"password": "any-password"}, follow_redirects=False)
        self.assertEqual(post.status_code, 303)
        self.assertEqual(post.headers["location"], "/login?pw_err=disabled")

    def test_oidc_user_validation_and_enable_flow(self):
        cookies = self._oidc_cookie()

        # 未设密码直接开启 -> 拦截并提示 empty
        r1 = self.client.post(
            "/settings",
            data={"theme": "default", "pw_login_enabled": "on", "pw_login_password": ""},
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(r1.headers["location"], "/settings?saved=1&pw_err=empty")
        self.assertFalse(password_auth.is_enabled())

        # 密码太短 -> 拦截并提示 short
        r2 = self.client.post(
            "/settings",
            data={"theme": "default", "pw_login_enabled": "on", "pw_login_password": "123"},
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(r2.headers["location"], "/settings?saved=1&pw_err=short")
        self.assertFalse(password_auth.is_enabled())

        # 合规密码开启 -> 成功，且 config.json 不含明文
        r3 = self.client.post(
            "/settings",
            data={"theme": "default", "pw_login_enabled": "on", "pw_login_password": "MyFixedPass99"},
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(r3.headers["location"], "/settings?saved=1")
        self.assertTrue(password_auth.is_enabled())
        raw_cfg = self.cfg_path.read_text(encoding="utf-8")
        self.assertNotIn("MyFixedPass99", raw_cfg)

        # 登录页出现密码入口
        login_html = self.client.get("/login").text
        self.assertIn("/login/password", login_html)

        # 错误密码 -> invalid，连续 3 次后限流
        for _ in range(3):
            bad = self.client.post("/login/password", data={"password": "wrong"}, follow_redirects=False)
            self.assertEqual(bad.headers["location"], "/login?pw_err=invalid")
        blocked = self.client.post("/login/password", data={"password": "MyFixedPass99"}, follow_redirects=False)
        self.assertEqual(blocked.headers["location"], "/login?pw_err=rate_limited")

        # 换一个 IP 用正确密码登录成功
        ok = self.client.post(
            "/login/password",
            data={"password": "MyFixedPass99"},
            headers={"X-Forwarded-For": "10.0.0.2"},
            follow_redirects=False,
        )
        self.assertEqual(ok.status_code, 303)
        self.assertEqual(ok.headers["location"], "/")
        session_cookie = ok.cookies.get(oidc.SESSION_COOKIE)
        self.assertIsNotNone(session_cookie)
        self.assertEqual(oidc.read_session(session_cookie)["auth_method"], "password")

        # 密码登录的会话能打开主页和设置页，但不能修改密码登录配置
        pw_cookies = {oidc.SESSION_COOKIE: session_cookie}
        self.assertEqual(self.client.get("/", cookies=pw_cookies).status_code, 200)
        settings_html = self.client.get("/settings", cookies=pw_cookies).text
        self.assertIn("仅支持在 Pocket ID 登录后开启、关闭或修改密码", settings_html)

        old_hash = config.load_config()["password_login"]["password_hash"]
        self.client.post(
            "/settings",
            data={"theme": "green", "pw_login_password": "hacked123456"},
            cookies=pw_cookies,
            follow_redirects=False,
        )
        after_cfg = config.load_config()
        self.assertEqual(after_cfg["theme"], "green")
        self.assertTrue(after_cfg["password_login"]["enabled"])
        self.assertEqual(after_cfg["password_login"]["password_hash"], old_hash)

        # Pocket ID 用户留空密码保存 -> 保留原哈希；关闭开关 -> 隐藏入口并拒绝密码登录
        self.client.post(
            "/settings",
            data={"theme": "green", "pw_login_enabled": "on", "pw_login_password": ""},
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(config.load_config()["password_login"]["password_hash"], old_hash)

        self.client.post(
            "/settings",
            data={"theme": "green", "pw_login_password": ""},
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertFalse(password_auth.is_enabled())
        self.assertNotIn("/login/password", self.client.get("/login").text)

    def test_password_login_works_when_oidc_temporarily_down(self):
        cookies = self._oidc_cookie()
        self.client.post(
            "/settings",
            data={"theme": "default", "pw_login_enabled": "on", "pw_login_password": "BackupPass123"},
            cookies=cookies,
            follow_redirects=False,
        )
        with patch.object(oidc, "build_authorize_url", new=AsyncMock(side_effect=RuntimeError("OIDC down"))):
            resp = self.client.get("/login")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Pocket ID 暂时不可用", resp.text)
            self.assertIn("/login/password", resp.text)


if __name__ == "__main__":
    unittest.main()
