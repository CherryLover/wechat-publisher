"""已有配图跳过 AI 生成（含图片数量阈值）+ 登录页密码明文切换测试"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import article, auth, config, imagegen, oidc, tenant, web


class TestSkipExistingImages(unittest.TestCase):
    def test_count_and_has_existing_images_detection(self):
        self.assertEqual(imagegen.count_existing_images(""), 0)
        self.assertEqual(
            imagegen.count_existing_images("纯文字段落，带一个普通链接 [文档](https://example.com)"),
            0,
        )
        one_img = "前文\n\n![封面](https://example.com/a.jpg)\n\n后文"
        self.assertEqual(imagegen.count_existing_images(one_img), 1)
        self.assertTrue(imagegen.has_existing_images(one_img, min_count=1))
        self.assertFalse(imagegen.has_existing_images(one_img, min_count=2))

        two_imgs = '![图1](https://example.com/a.jpg)\n\n<img src="https://example.com/b.png" />'
        self.assertEqual(imagegen.count_existing_images(two_imgs), 2)
        self.assertTrue(imagegen.has_existing_images(two_imgs, min_count=2))
        self.assertFalse(imagegen.has_existing_images(two_imgs, min_count=3))

    def test_build_skips_only_when_image_count_reaches_threshold(self):
        # 默认阈值 2 张：2 张图直接跳过，不调规划与生图
        cfg_default = {
            "enabled": True,
            "api_key": "sk-test",
            "skip_if_has_images": True,
        }
        md_two = "# 标题\n\n![图1](https://example.com/1.png)\n\n![图2](https://example.com/2.png)\n\n正文"
        mock_plan = AsyncMock()
        mock_gen = AsyncMock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(imagegen, "get_cfg", return_value=cfg_default),
            patch.object(imagegen, "plan_prompts", mock_plan),
            patch.object(imagegen, "generate_image", mock_gen),
        ):
            res = asyncio.run(imagegen.build("标题", md_two, ref="page1", upload_dir=Path(tmp)))
        self.assertTrue(res.skipped_existing_images)
        self.assertEqual(res.markdown, md_two)
        self.assertIsNone(res.cover_bytes)
        self.assertEqual(res.items, [])
        mock_plan.assert_not_called()
        mock_gen.assert_not_called()

        # 默认阈值 2 张：只有 1 张图时不跳过，正常进入规划
        md_one = "# 标题\n\n![图1](https://example.com/1.png)\n\n正文"
        plan_stub = {"cover": {"prompt": "p"}, "illustrations": []}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(imagegen, "get_cfg", return_value=cfg_default),
            patch.object(imagegen, "plan_prompts", AsyncMock(return_value=plan_stub)) as m_plan_one,
            patch.object(imagegen, "generate_image", AsyncMock(side_effect=RuntimeError("stop"))),
        ):
            res_one = asyncio.run(imagegen.build("标题", md_one, ref="page1b", upload_dir=Path(tmp)))
        self.assertFalse(res_one.skipped_existing_images)
        m_plan_one.assert_called_once()

        # 自定义阈值 3 张：2 张不跳过，3 张跳过
        cfg_three = {
            "enabled": True,
            "api_key": "sk-test",
            "skip_if_has_images": True,
            "skip_min_images": 3,
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(imagegen, "get_cfg", return_value=cfg_three),
            patch.object(imagegen, "plan_prompts", AsyncMock(return_value=plan_stub)) as m_plan_two,
            patch.object(imagegen, "generate_image", AsyncMock(side_effect=RuntimeError("stop"))),
        ):
            res_two = asyncio.run(imagegen.build("标题", md_two, ref="page2", upload_dir=Path(tmp)))
        self.assertFalse(res_two.skipped_existing_images)
        m_plan_two.assert_called_once()

        md_three = md_two + "\n\n![图3](https://example.com/3.png)"
        mock_plan3 = AsyncMock()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(imagegen, "get_cfg", return_value=cfg_three),
            patch.object(imagegen, "plan_prompts", mock_plan3),
        ):
            res_three = asyncio.run(imagegen.build("标题", md_three, ref="page3", upload_dir=Path(tmp)))
        self.assertTrue(res_three.skipped_existing_images)
        mock_plan3.assert_not_called()

    def test_build_runs_normally_when_skip_disabled(self):
        cfg_no_skip = {
            "enabled": True,
            "api_key": "sk-test",
            "skip_if_has_images": False,
            "skip_min_images": 1,
        }
        plan_stub = {"cover": {"prompt": "p"}, "illustrations": []}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(imagegen, "get_cfg", return_value=cfg_no_skip),
            patch.object(imagegen, "plan_prompts", AsyncMock(return_value=plan_stub)) as m1,
            patch.object(imagegen, "generate_image", AsyncMock(side_effect=RuntimeError("stop"))),
        ):
            res1 = asyncio.run(
                imagegen.build(
                    "标题",
                    "![图1](https://a.com/1.jpg)\n![图2](https://a.com/2.jpg)",
                    ref="p1",
                    upload_dir=Path(tmp),
                )
            )
        self.assertFalse(res1.skipped_existing_images)
        m1.assert_called_once()


class TestSettingsAndLoginEye(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp_dir = Path(self._tmp.name)
        self.cfg_path = tmp_dir / "config.json"
        self.db_path = tmp_dir / "articles.db"
        self.p_cfg = patch.object(config, "CONFIG_PATH", self.cfg_path)
        self.p_db = patch.object(tenant, "DB_PATH", self.db_path)
        self.p_art_db = patch.object(article, "DB_PATH", self.db_path)
        self.p_tok_db = patch.object(auth, "_TOKEN_DB_PATH", self.db_path)
        self.p_secret = patch.object(oidc, "SESSION_SECRET", "unit-test-secret")
        self.p_issuer = patch.object(oidc, "ISSUER", "https://id.example.com")
        self.p_cid = patch.object(oidc, "CLIENT_ID", "cid")
        self.p_csec = patch.object(oidc, "CLIENT_SECRET", "csec")
        self.p_redir = patch.object(oidc, "REDIRECT_URI", "https://pub.example.com/auth/callback")
        self.p_auth_url = patch.object(oidc, "build_authorize_url", new=AsyncMock(return_value="https://id.example.com/auth"))
        self.p_articles = patch.object(web.article, "list_articles", return_value=[])
        self.p_preview = patch.object(web, "create_preview_token", return_value={"token": "tok"})
        for p in (
            self.p_cfg, self.p_db, self.p_art_db, self.p_tok_db,
            self.p_secret, self.p_issuer, self.p_cid, self.p_csec,
            self.p_redir, self.p_auth_url, self.p_articles, self.p_preview,
        ):
            p.start()

        app = FastAPI()
        app.include_router(web.router)
        self.client = TestClient(app)

    def tearDown(self):
        for p in (
            self.p_preview, self.p_articles, self.p_auth_url, self.p_redir,
            self.p_csec, self.p_cid, self.p_issuer, self.p_secret,
            self.p_tok_db, self.p_art_db, self.p_db, self.p_cfg,
        ):
            p.stop()
        self._tmp.cleanup()

    def test_settings_persists_skip_flag_and_min_count_and_login_renders_eye_toggle(self):
        tenant.bootstrap_owner()
        self.assertEqual(tenant.get_tenant_config("owner")["ai_images"]["skip_min_images"], 2)

        cookies = {oidc.SESSION_COOKIE: oidc.create_session({"id": "owner", "username": "admin", "role": "admin", "sub": "u1", "name": "Owner", "auth_method": "oidc"})}
        resp = self.client.post(
            "/settings",
            data={
                "theme": "default",
                "ai_enabled": "on",
                "ai_skip_if_has_images": "on",
                "ai_skip_min_images": "3",
                "new_password": "Password123",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        saved_ai = tenant.get_tenant_config("owner")["ai_images"]
        self.assertTrue(saved_ai["skip_if_has_images"])
        self.assertEqual(saved_ai["skip_min_images"], 3)

        # 0 钳位为 1
        self.client.post(
            "/settings",
            data={"theme": "default", "ai_skip_if_has_images": "on", "ai_skip_min_images": "0"},
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(tenant.get_tenant_config("owner")["ai_images"]["skip_min_images"], 1)

        # 非法数值兜底为 2
        self.client.post(
            "/settings",
            data={"theme": "default", "ai_skip_if_has_images": "on", "ai_skip_min_images": "abc"},
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(tenant.get_tenant_config("owner")["ai_images"]["skip_min_images"], 2)

        settings_html = self.client.get("/settings", cookies=cookies).text
        self.assertIn('name="ai_skip_if_has_images"', settings_html)
        self.assertIn('name="ai_skip_min_images"', settings_html)

        login_html = self.client.get("/login").text
        self.assertIn('id="pw-toggle"', login_html)
        self.assertIn('id="pw-input"', login_html)


if __name__ == "__main__":
    unittest.main()
