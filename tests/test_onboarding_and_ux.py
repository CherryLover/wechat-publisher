"""新手引导向导、实时连通性校验、Notion 链接解析、示例主题预览与 7 天滑动会话刷新测试"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from src import article, auth, config, main as app_main, notion_db, oidc, password_auth, tenant, usage, web


def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=app_main.STATIC_DIR), name="static")
    app.add_api_route("/favicon.ico", app_main.favicon_ico, methods=["GET"])
    app.add_api_route("/favicon.svg", app_main.favicon_svg, methods=["GET"])
    app.middleware("http")(app_main.sliding_session_middleware)
    app.include_router(web.router)
    return app


class TestOnboardingAndUX(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp_dir = Path(cls._tmp.name)
        cls.db_path = tmp_dir / "articles.db"
        cls.cfg_path = tmp_dir / "config.json"
        cls.upload_dir = tmp_dir / "uploads"
        cls.upload_dir.mkdir(parents=True, exist_ok=True)
        cls.cache_path = tmp_dir / "notion_cache.json"

        cls.patches = [
            patch.object(config, "CONFIG_PATH", cls.cfg_path),
            patch.object(tenant, "DB_PATH", cls.db_path),
            patch.object(article, "DB_PATH", cls.db_path),
            patch.object(auth, "_TOKEN_DB_PATH", cls.db_path),
            patch.object(usage, "DB_PATH", cls.db_path),
            patch.object(notion_db, "CACHE_PATH", cls.cache_path),
            patch.object(app_main, "UPLOAD_DIR", cls.upload_dir),
            patch.object(oidc, "SESSION_SECRET", "onboarding-test-secret"),
        ]
        for p in cls.patches:
            p.start()

        tenant._wechat_pool.clear()
        notion_db.clear_cache()
        cls.client = TestClient(_build_test_app())

    @classmethod
    def tearDownClass(cls):
        tenant._wechat_pool.clear()
        notion_db.clear_cache()
        for p in reversed(cls.patches):
            p.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.client.cookies.clear()
        tenant._wechat_pool.clear()
        notion_db.clear_cache()
        if self.cfg_path.exists():
            self.cfg_path.unlink()
        if self.db_path.exists():
            import sqlite3
            conn = sqlite3.connect(str(self.db_path))
            for tbl in ("articles", "api_usage", "preview_tokens", "ai_style_log"):
                try:
                    conn.execute(f"DELETE FROM {tbl}")
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute("DELETE FROM users WHERE id != 'owner'")
            except sqlite3.OperationalError:
                pass
            conn.commit()
            conn.close()
        tenant.bootstrap_owner()

    def test_notion_database_id_extraction(self):
        self.assertEqual(
            notion_db.extract_database_id(
                "https://www.notion.so/workspace/My-DB-2c1f8a9b0c124e3f9a1b2c3d4e5f6789?v=abcd1234abcd1234abcd1234abcd1234"
            ),
            "2c1f8a9b0c124e3f9a1b2c3d4e5f6789",
        )
        self.assertEqual(
            notion_db.extract_database_id("2c1f8a9b-0c12-4e3f-9a1b-2c3d4e5f6789"),
            "2c1f8a9b0c124e3f9a1b2c3d4e5f6789",
        )
        self.assertEqual(notion_db.extract_database_id("custom-short-id"), "custom-short-id")

    def test_register_prefills_author_and_forces_onboarding_until_wechat_verified(self):
        r = self.client.post(
            "/register",
            data={
                "username": "newauthor",
                "display_name": "科技漫游",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/onboarding?welcome=1")
        ck = {oidc.SESSION_COOKIE: r.cookies.get(oidc.SESSION_COOKIE)}
        self.client.cookies.clear()

        # 未验证微信公众号前，访问核心页一律进入 /onboarding
        for path in ("/", "/source", "/flow", "/usage"):
            resp = self.client.get(path, cookies=ck, follow_redirects=False)
            self.assertEqual(resp.status_code, 303, f"path={path}")
            self.assertEqual(resp.headers["location"], "/onboarding")

        # 引导页自动带入注册时填的公众号名称，且不含“专属接入链接”与“修改登录密码”卡片
        ob_html = self.client.get("/onboarding", cookies=ck).text
        self.assertIn('value="科技漫游"', ob_html)
        self.assertNotIn("专属接入链接", ob_html)
        self.assertNotIn("修改登录密码", ob_html)

        # 即时测试公众号连通性失败（不保存直接测试，含 40164 引导）
        with patch.object(web.WechatAPI, "get_access_token", AsyncMock(side_effect=RuntimeError("errcode 40164 invalid ip"))):
            r_test_fail = self.client.post(
                "/settings/test-wechat",
                json={"appid": "wx_bad", "appsecret": "sec_bad"},
                cookies=ck,
            )
            self.assertFalse(r_test_fail.json()["ok"])
            self.assertIn("白名单", r_test_fail.json()["reason"])

            # 校验未通过时，不允许保存通过第 1 步
            r_save_fail = self.client.post(
                "/onboarding/step/wechat",
                json={"appid": "wx_bad", "appsecret": "sec_bad", "author": "科技漫游"},
                cookies=ck,
            )
            self.assertEqual(r_save_fail.status_code, 400)
            self.assertFalse(r_save_fail.json()["ok"])
            self.assertTrue(tenant.needs_onboarding(oidc.read_session(ck[oidc.SESSION_COOKIE])["uid"]))

        # 校验通过后保存成功，解锁工作台并加密存储 AppSecret，进入第 2 步 connect
        with (
            patch.object(web.WechatAPI, "get_access_token", AsyncMock(return_value="wx_ok_token")),
            patch.object(web.WechatAPI, "try_fetch_account_author", AsyncMock(return_value="")),
        ):
            r_save_ok = self.client.post(
                "/onboarding/step/wechat",
                json={"appid": "wx_real_01", "appsecret": "sec_real_01", "author": "科技漫游"},
                cookies=ck,
            )
        self.assertEqual(r_save_ok.status_code, 200)
        d_ok = r_save_ok.json()
        self.assertTrue(d_ok["ok"])
        self.assertTrue(d_ok["encrypted_now"])
        self.assertEqual(d_ok["next_step"], "connect")
        self.assertIn("AES-256-GCM", d_ok["message"])
        self.assertIn("/mcp/mcp_", d_ok["mcp_url"])
        self.assertIn("/skill/mcp_", d_ok["skill_url"])
        self.assertIn("wechat-publish.zip", d_ok["skill_zip_url"])
        self.assertIn("MCP", d_ok["bundle_text"])

        uid = oidc.read_session(ck[oidc.SESSION_COOKIE])["uid"]
        self.assertFalse(tenant.needs_onboarding(uid))
        self.assertEqual(tenant.get_tenant_config(uid)["wechat"]["author"], "科技漫游")

        # 第 2 步：继续 / 跳过到 notion，第 3 步测试 Notion 链接并拉取最近文章列表
        self.assertEqual(self.client.post("/onboarding/step/connect", json={"action": "next"}, cookies=ck).json()["next_step"], "notion")
        sample_pages = [
            {
                "id": "p1",
                "last_edited_time": "2026-11-27T08:30:00.000Z",
                "properties": {"标题": {"type": "title", "title": [{"plain_text": "第一篇测试文章"}]}, "状态": {"type": "select", "select": {"name": "草稿"}}},
            }
        ]
        with (
            patch.object(notion_db.NotionSource, "get_database", AsyncMock(return_value={"title": [{"plain_text": "灵感库"}], "properties": {"标题": {}}})),
            patch.object(notion_db.NotionSource, "query_database", AsyncMock(return_value=(sample_pages, False))),
        ):
            r_nt_test = self.client.post(
                "/settings/test-notion",
                json={
                    "token": "ntn_live_tok",
                    "db_id": "https://www.notion.so/team/aa11bb22cc33dd44ee55ff6677889900?v=1234567890abcdef1234567890abcdef",
                },
                cookies=ck,
            )
        d_nt = r_nt_test.json()
        self.assertTrue(d_nt["ok"])
        self.assertFalse(d_nt["empty"])
        self.assertEqual(d_nt["db_id"], "aa11bb22cc33dd44ee55ff6677889900")
        self.assertEqual(len(d_nt["articles"]), 1)
        self.assertEqual(d_nt["articles"][0]["title"], "第一篇测试文章")

        # 若数据库无文章，明确返回 empty=True 与空提示
        with (
            patch.object(notion_db.NotionSource, "get_database", AsyncMock(return_value={"title": [{"plain_text": "空库"}], "properties": {}})),
            patch.object(notion_db.NotionSource, "query_database", AsyncMock(return_value=([], False))),
        ):
            r_nt_empty = self.client.post(
                "/settings/test-notion",
                json={"token": "ntn_live_tok", "db_id": "aa11bb22cc33dd44ee55ff6677889900"},
                cookies=ck,
            )
        self.assertTrue(r_nt_empty.json()["ok"])
        self.assertTrue(r_nt_empty.json()["empty"])
        self.assertIn("为空", r_nt_empty.json()["message"])

        # 第 2 步可选跳过 / 第 3-5 步可跳过，第 5 步完成返回 /
        self.assertEqual(self.client.post("/onboarding/step/ai", json={"action": "skip"}, cookies=ck).json()["next_step"], "theme")
        r_th = self.client.post("/onboarding/step/theme", json={"theme": "lapis"}, cookies=ck)
        self.assertEqual(r_th.json()["next_step"], "notify")

        # 第 5 步 Bark 不先保存直接测试通知
        with patch("src.notify.httpx.AsyncClient.post", AsyncMock(return_value=type("R", (), {"status_code": 200, "text": "ok"})())):
            r_bark_test = self.client.post(
                "/settings/test-notify",
                json={"key": "bark_unsaved_key", "server": "https://api.day.app", "group": "Test"},
                cookies=ck,
            )
        self.assertTrue(r_bark_test.json()["ok"])

        r_done = self.client.post("/onboarding/step/notify", json={"action": "skip"}, cookies=ck)
        self.assertTrue(r_done.json()["done"])
        self.assertEqual(self.client.get("/", cookies=ck, follow_redirects=False).status_code, 200)

        # 示例主题预览路由工作
        demo_html = self.client.get("/preview-demo?theme=lapis", cookies=ck)
        self.assertEqual(demo_html.status_code, 200)
        self.assertIn("示例文案", demo_html.text)

    def test_7_day_session_and_sliding_refresh(self):
        self.assertEqual(oidc.SESSION_TTL, 7 * 24 * 3600)

        self.client.post(
            "/register",
            data={
                "username": "sessionuser",
                "display_name": "SessionTest",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )
        u = tenant.get_user_by_id("owner")
        # 伪造一个签发于 13 小时前但尚未过期的 7 天会话
        self.client.cookies.clear()
        old_iat = int(time.time()) - (13 * 3600)
        with patch("src.oidc.time.time", return_value=old_iat):
            stale_cookie = oidc.create_session({
                "id": u["id"],
                "uid": u["id"],
                "username": u["username"],
                "role": u["role"],
                "name": "Owner",
                "auth_method": "password",
            })

        sess = oidc.read_session(stale_cookie)
        self.assertIsNotNone(sess)
        self.assertTrue(oidc.should_refresh_session(sess))

        # 访问页面时中间件自动下发续期后的新 cookie
        resp = self.client.get("/onboarding", cookies={oidc.SESSION_COOKIE: stale_cookie})
        self.assertEqual(resp.status_code, 200)
        new_cookie = resp.cookies.get(oidc.SESSION_COOKIE)
        self.assertIsNotNone(new_cookie)
        self.assertNotEqual(new_cookie, stale_cookie)
        new_sess = oidc.read_session(new_cookie)
        self.assertFalse(oidc.should_refresh_session(new_sess))

    def test_static_icons_and_favicon_served(self):
        self.assertEqual(self.client.get("/favicon.svg").status_code, 200)
        self.assertEqual(self.client.get("/favicon.ico").status_code, 200)
        self.assertEqual(self.client.get("/static/lucide.min.js").status_code, 200)

    def test_main_loads_dotenv_before_local_module_imports(self):
        main_src = Path(app_main.__file__).read_text(encoding="utf-8")
        dotenv_pos = main_src.find("load_dotenv()")
        local_import_pos = main_src.find("from . import")
        self.assertNotEqual(dotenv_pos, -1)
        self.assertNotEqual(local_import_pos, -1)
        self.assertLess(dotenv_pos, local_import_pos)

    def test_idempotent_secret_encryption_and_wechat_author_autofill(self):
        self.client.cookies.clear()
        r = self.client.post(
            "/register",
            data={
                "username": "idemuser",
                "display_name": "",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )
        ck = {oidc.SESSION_COOKIE: r.cookies.get(oidc.SESSION_COOKIE)}
        uid = oidc.read_session(ck[oidc.SESSION_COOKIE])["uid"]
        self.client.cookies.clear()

        # 留空作者时自动尝试拉取公众号作者名
        with (
            patch.object(web.WechatAPI, "get_access_token", AsyncMock(return_value="wx_ok")),
            patch.object(web.WechatAPI, "try_fetch_account_author", AsyncMock(return_value="自动作者君")),
        ):
            r1 = self.client.post(
                "/onboarding/step/wechat",
                json={"appid": "wx_idem_1", "appsecret": "sec_idem_1", "author": ""},
                cookies=ck,
            )
        self.assertEqual(r1.status_code, 200)
        d1 = r1.json()
        self.assertTrue(d1["encrypted_now"])
        self.assertEqual(d1["author"], "自动作者君")

        # 从 SQLite 直接读取加密后密文，然后再次提交/保存其他配置，密文不得变动
        import json
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        raw_before = json.loads(conn.execute("SELECT config_json FROM users WHERE id = ?", (uid,)).fetchone()[0])
        conn.close()
        cipher_before = raw_before["wechat"]["appsecret"]
        self.assertTrue(cipher_before.startswith("enc:v1:"))

        # 再次回到第 1 步直接保存（密钥未变更），不应重新加密且 encrypted_now=False
        r2 = self.client.post(
            "/onboarding/step/wechat",
            json={"appid": "wx_idem_1", "appsecret": "", "author": "自动作者君"},
            cookies=ck,
        )
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r2.json()["encrypted_now"])

        # 保存 Notion 或 Theme 步骤时，微信原密文字节保持完全一致
        self.client.post("/onboarding/step/theme", json={"theme": "orangeheart"}, cookies=ck)
        conn = sqlite3.connect(str(self.db_path))
        raw_after = json.loads(conn.execute("SELECT config_json FROM users WHERE id = ?", (uid,)).fetchone()[0])
        conn.close()
        self.assertEqual(raw_after["wechat"]["appsecret"], cipher_before)

    def test_tenant_skill_endpoints_and_isolation(self):
        import io
        import zipfile

        self.client.cookies.clear()
        r = self.client.post(
            "/register",
            data={
                "username": "skilluser",
                "display_name": "Skill号",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )
        ck = {oidc.SESSION_COOKIE: r.cookies.get(oidc.SESSION_COOKIE)}
        uid = oidc.read_session(ck[oidc.SESSION_COOKIE])["uid"]
        self.client.cookies.clear()

        with (
            patch.object(web.WechatAPI, "get_access_token", AsyncMock(return_value="wx_ok")),
            patch.object(web.WechatAPI, "try_fetch_account_author", AsyncMock(return_value="")),
        ):
            self.client.post(
                "/onboarding/step/wechat",
                json={"appid": "wx_sk_1", "appsecret": "sec_sk_1", "author": "Skill号"},
                cookies=ck,
            )

        u = tenant.get_user_by_id(uid)
        tok = u["mcp_token"]
        self.assertTrue(tok.startswith("mcp_"))

        # Skill Markdown / Python 脚本 / zip 压缩包均按租户 token 正确返回
        r_md = self.client.get(f"/skill/{tok}/SKILL.md")
        self.assertEqual(r_md.status_code, 200)
        self.assertIn(f"/mcp/{tok}", r_md.text)
        self.assertIn("/api/articles", r_md.text)
        self.assertIn("publish_to_draft", r_md.text)

        r_py = self.client.get(f"/skill/{tok}/scripts/publish.py")
        self.assertEqual(r_py.status_code, 200)
        self.assertIn(tok, r_py.text)

        r_zip = self.client.get(f"/skill/{tok}/wechat-publish.zip")
        self.assertEqual(r_zip.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(r_zip.content)) as zf:
            names = set(zf.namelist())
            self.assertIn("wechat-publish/SKILL.md", names)
            self.assertIn("wechat-publish/scripts/publish.py", names)

        # 非法 token -> 401；停用账号 -> 403
        self.assertEqual(self.client.get("/skill/mcp_invalid_999/SKILL.md").status_code, 401)
        tenant.set_user_status(uid, "disabled")
        self.assertEqual(self.client.get(f"/skill/{tok}/SKILL.md").status_code, 403)


if __name__ == "__main__":
    unittest.main()
