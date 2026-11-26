"""已有配图跳过 AI 生成 + 登录页密码明文切换测试"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import config, imagegen, oidc, web


class TestSkipExistingImages(unittest.TestCase):
    def test_has_existing_images_detection(self):
        self.assertTrue(imagegen.has_existing_images("前文\n\n![封面](https://example.com/a.jpg)\n\n后文"))
        self.assertTrue(imagegen.has_existing_images('一段文字 <img src="https://example.com/b.png" />'))
        self.assertFalse(imagegen.has_existing_images("纯文字段落，带一个普通链接 [文档](https://example.com)"))
        self.assertFalse(imagegen.has_existing_images(""))

    def test_build_skips_when_enabled_and_markdown_has_images(self):
        cfg = {
            "enabled": True,
            "api_key": "sk-test",
            "skip_if_has_images": True,
        }
        md = "# 标题\n\n![已有配图](https://example.com/pic.png)\n\n正文"
        mock_plan = AsyncMock()
        mock_gen = AsyncMock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(imagegen, "get_cfg", return_value=cfg),
            patch.object(imagegen, "plan_prompts", mock_plan),
            patch.object(imagegen, "generate_image", mock_gen),
        ):
            res = asyncio.run(imagegen.build("标题", md, ref="page1", upload_dir=Path(tmp)))
        self.assertTrue(res.skipped_existing_images)
        self.assertEqual(res.markdown, md)
        self.assertIsNone(res.cover_bytes)
        self.assertEqual(res.items, [])
        mock_plan.assert_not_called()
        mock_gen.assert_not_called()

    def test_build_runs_normally_when_skip_disabled_or_no_images(self):
        cfg_no_skip = {"enabled": True, "api_key": "sk-test", "skip_if_has_images": False}
        cfg_skip = {"enabled": True, "api_key": "sk-test", "skip_if_has_images": True}
        plan_stub = {"cover": {"prompt": "p"}, "illustrations": []}

        # 开关关闭：即使有图也进入 plan_prompts
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(imagegen, "get_cfg", return_value=cfg_no_skip),
            patch.object(imagegen, "plan_prompts", AsyncMock(return_value=plan_stub)) as m1,
            patch.object(imagegen, "generate_image", AsyncMock(side_effect=RuntimeError("stop"))),
        ):
            res1 = asyncio.run(imagegen.build("标题", "![图](https://a.com/1.jpg)", ref="p1", upload_dir=Path(tmp)))
        self.assertFalse(res1.skipped_existing_images)
        m1.assert_called_once()

        # 开关开启但正文无图：正常进入 plan_prompts
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(imagegen, "get_cfg", return_value=cfg_skip),
            patch.object(imagegen, "plan_prompts", AsyncMock(return_value=plan_stub)) as m2,
            patch.object(imagegen, "generate_image", AsyncMock(side_effect=RuntimeError("stop"))),
        ):
            res2 = asyncio.run(imagegen.build("标题", "纯文本正文，没有图片", ref="p2", upload_dir=Path(tmp)))
        self.assertFalse(res2.skipped_existing_images)
        m2.assert_called_once()


class TestSettingsAndLoginEye(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self._tmp.name) / "config.json"
        self.p_cfg = patch.object(config, "CONFIG_PATH", self.cfg_path)
        self.p_secret = patch.object(oidc, "SESSION_SECRET", "unit-test-secret")
        self.p_issuer = patch.object(oidc, "ISSUER", "https://id.example.com")
        self.p_cid = patch.object(oidc, "CLIENT_ID", "cid")
        self.p_csec = patch.object(oidc, "CLIENT_SECRET", "csec")
        self.p_redir = patch.object(oidc, "REDIRECT_URI", "https://pub.example.com/auth/callback")
        self.p_auth_url = patch.object(oidc, "build_authorize_url", new=AsyncMock(return_value="https://id.example.com/auth"))
        self.p_articles = patch.object(web.article, "list_articles", return_value=[])
        self.p_preview = patch.object(web, "create_preview_token", return_value={"token": "tok"})
        for p in (
            self.p_cfg, self.p_secret, self.p_issuer, self.p_cid, self.p_csec,
            self.p_redir, self.p_auth_url, self.p_articles, self.p_preview,
        ):
            p.start()

        app = FastAPI()
        app.include_router(web.router)
        self.client = TestClient(app)

    def tearDown(self):
        for p in (
            self.p_preview, self.p_articles, self.p_auth_url, self.p_redir,
            self.p_csec, self.p_cid, self.p_issuer, self.p_secret, self.p_cfg,
        ):
            p.stop()
        self._tmp.cleanup()

    def test_settings_persists_skip_flag_and_login_renders_eye_toggle(self):
        cookies = {oidc.SESSION_COOKIE: oidc.create_session({"sub": "u1", "name": "Owner", "auth_method": "oidc"})}
        resp = self.client.post(
            "/settings",
            data={
                "theme": "default",
                "ai_enabled": "on",
                "ai_skip_if_has_images": "on",
                "pw_login_enabled": "on",
                "pw_login_password": "Password123",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(config.load_config()["ai_images"]["skip_if_has_images"])

        settings_html = self.client.get("/settings", cookies=cookies).text
        self.assertIn('name="ai_skip_if_has_images"', settings_html)

        login_html = self.client.get("/login").text
        self.assertIn('id="pw-toggle"', login_html)
        self.assertIn('id="pw-input"', login_html)


if __name__ == "__main__":
    unittest.main()
