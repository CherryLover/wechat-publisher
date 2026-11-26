"""设置页与管理后台敏感密钥（微信 AppSecret / Notion Token / Bark 设备 Key / 平台 AI API Key）只写不读测试"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import article, auth, config, oidc, tenant, usage, web


class TestSettingsSecretsWriteOnly(unittest.TestCase):
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
            patch.object(usage, "DB_PATH", self.db_path),
            patch.object(oidc, "SESSION_SECRET", "unit-test-secret"),
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

    def _admin_cookies(self) -> dict[str, str]:
        tenant.bootstrap_owner()
        return {
            oidc.SESSION_COOKIE: oidc.create_session(
                {"id": "owner", "username": "admin", "role": "admin", "sub": "u1", "name": "Owner", "auth_method": "oidc"}
            )
        }

    def test_tenant_and_platform_secrets_are_never_rendered_and_can_be_updated(self):
        cookies = self._admin_cookies()

        # 1) 设置页提交租户密钥
        r1 = self.client.post(
            "/settings",
            data={
                "theme": "default",
                "wx_appid": "wx123456",
                "wx_appsecret": "SUPER_WX_SECRET_999",
                "notion_token": "ntn_SUPER_SECRET_777",
                "notion_db_id": "db-111",
                "notion_webhook_secret": "wh_sec_888",
                "notify_enabled": "on",
                "bark_key": "SUPER_SECRET_BARK_KEY_123",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(r1.status_code, 303)

        # 2) 管理后台提交平台 AI API Key
        r_admin = self.client.post(
            "/admin/platform",
            data={
                "allow_registration": "on",
                "ai_base_url": "https://api.example.com/v1",
                "ai_api_key": "sk-SUPER-SECRET-AI-KEY-456",
                "ai_text_model": "gpt-text",
                "ai_image_model": "gpt-img",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(r_admin.status_code, 303)

        # 3) 页面 HTML 中不得出现任何密钥明文，且展示“已设置”
        html = self.client.get("/settings", cookies=cookies).text
        for secret in ("SUPER_WX_SECRET_999", "ntn_SUPER_SECRET_777", "wh_sec_888", "SUPER_SECRET_BARK_KEY_123", "sk-SUPER-SECRET-AI-KEY-456"):
            self.assertNotIn(secret, html)
        self.assertIn("已设置，留空保持不变", html)

        admin_html = self.client.get("/admin", cookies=cookies).text
        self.assertNotIn("sk-SUPER-SECRET-AI-KEY-456", admin_html)
        self.assertIn("已设置，留空保持不变", admin_html)

        # 4) 留空提交保存：原密钥保持不变
        self.client.post(
            "/settings",
            data={"theme": "green", "wx_appid": "wx123456", "wx_appsecret": "", "notion_token": "", "bark_key": ""},
            cookies=cookies,
            follow_redirects=False,
        )
        self.client.post(
            "/admin/platform",
            data={"allow_registration": "on", "ai_base_url": "https://api.example.com/v1", "ai_api_key": ""},
            cookies=cookies,
            follow_redirects=False,
        )
        tcfg = tenant.get_tenant_config("owner")
        self.assertEqual(tcfg["wechat"]["appsecret"], "SUPER_WX_SECRET_999")
        self.assertEqual(tcfg["notion"]["token"], "ntn_SUPER_SECRET_777")
        self.assertEqual(tcfg["notify"]["bark"]["key"], "SUPER_SECRET_BARK_KEY_123")
        self.assertEqual(config.load_config()["ai_images"]["api_key"], "sk-SUPER-SECRET-AI-KEY-456")

        # 5) 填入新密钥更新：覆盖旧值且依然不回显
        self.client.post(
            "/settings",
            data={"theme": "green", "wx_appid": "wx123456", "wx_appsecret": "NEW_WX_SEC", "bark_key": "NEW_BARK_KEY_789"},
            cookies=cookies,
            follow_redirects=False,
        )
        self.client.post(
            "/admin/platform",
            data={"allow_registration": "on", "ai_base_url": "https://api.example.com/v1", "ai_api_key": "sk-NEW-AI-KEY-000"},
            cookies=cookies,
            follow_redirects=False,
        )
        tcfg2 = tenant.get_tenant_config("owner")
        self.assertEqual(tcfg2["wechat"]["appsecret"], "NEW_WX_SEC")
        self.assertEqual(tcfg2["notify"]["bark"]["key"], "NEW_BARK_KEY_789")
        self.assertEqual(config.load_config()["ai_images"]["api_key"], "sk-NEW-AI-KEY-000")

        html2 = self.client.get("/settings", cookies=cookies).text
        self.assertNotIn("NEW_WX_SEC", html2)
        self.assertNotIn("NEW_BARK_KEY_789", html2)
        self.assertNotIn("sk-NEW-AI-KEY-000", html2)


if __name__ == "__main__":
    unittest.main()
