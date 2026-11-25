"""设置页敏感密钥（Bark 设备 Key / AI API Key）只写不读测试"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import config, oidc, web


class TestSettingsSecretsWriteOnly(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self._tmp.name) / "config.json"
        self.p_cfg = patch.object(config, "CONFIG_PATH", self.cfg_path)
        self.p_secret = patch.object(oidc, "SESSION_SECRET", "unit-test-secret")
        self.p_articles = patch.object(web.article, "list_articles", return_value=[])
        self.p_preview = patch.object(web, "create_preview_token", return_value={"token": "tok"})
        for p in (self.p_cfg, self.p_secret, self.p_articles, self.p_preview):
            p.start()

        app = FastAPI()
        app.include_router(web.router)
        self.client = TestClient(app)

    def tearDown(self):
        for p in (self.p_preview, self.p_articles, self.p_secret, self.p_cfg):
            p.stop()
        self._tmp.cleanup()

    def _cookies(self) -> dict[str, str]:
        return {oidc.SESSION_COOKIE: oidc.create_session({"sub": "u1", "name": "Owner", "auth_method": "oidc"})}

    def test_bark_key_and_ai_api_key_are_never_rendered_and_can_be_updated(self):
        cookies = self._cookies()

        # 1) 首次设置两个密钥
        r1 = self.client.post(
            "/settings",
            data={
                "theme": "default",
                "notify_enabled": "on",
                "bark_key": "SUPER_SECRET_BARK_KEY_123",
                "ai_enabled": "on",
                "ai_api_key": "sk-SUPER-SECRET-AI-KEY-456",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(r1.status_code, 303)

        # 2) 打开设置页：HTML 中不得出现任何密钥明文，但应展示“已设置”
        html = self.client.get("/settings", cookies=cookies).text
        self.assertNotIn("SUPER_SECRET_BARK_KEY_123", html)
        self.assertNotIn("sk-SUPER-SECRET-AI-KEY-456", html)
        self.assertIn("已设置，留空保持不变", html)

        # 3) 留空提交保存：原密钥保持不变
        self.client.post(
            "/settings",
            data={"theme": "green", "bark_key": "", "ai_api_key": ""},
            cookies=cookies,
            follow_redirects=False,
        )
        saved = config.load_config()
        self.assertEqual(saved["notify"]["bark"]["key"], "SUPER_SECRET_BARK_KEY_123")
        self.assertEqual(saved["ai_images"]["api_key"], "sk-SUPER-SECRET-AI-KEY-456")

        # 4) 填入新密钥更新：覆盖旧值且依然不回显
        self.client.post(
            "/settings",
            data={"theme": "green", "bark_key": "NEW_BARK_KEY_789", "ai_api_key": "sk-NEW-AI-KEY-000"},
            cookies=cookies,
            follow_redirects=False,
        )
        updated = config.load_config()
        self.assertEqual(updated["notify"]["bark"]["key"], "NEW_BARK_KEY_789")
        self.assertEqual(updated["ai_images"]["api_key"], "sk-NEW-AI-KEY-000")

        html2 = self.client.get("/settings", cookies=cookies).text
        self.assertNotIn("NEW_BARK_KEY_789", html2)
        self.assertNotIn("sk-NEW-AI-KEY-000", html2)


if __name__ == "__main__":
    unittest.main()
