"""多租户 SaaS V1 核心功能端到端测试：
1. 对称认证加密与防篡改
2. 注册登录 -> 微信配置触发随机种子派生 MCP/Webhook 链接 -> 重置种子作废旧链接
3. 文章、MCP (/mcp/{token})、Notion Webhook (/notion/webhook/{token}) 严格租户隔离
4. 管理员后台 /admin 权限控制、用户停用立即阻断（Web / MCP / Webhook）与恢复
5. 站内预览双图分离 + 微信图床代理白名单防 SSRF
6. 平台统一 AI 绘图配置 + 用户偏好合并 + 免费公告展示
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src import (
    article,
    auth,
    config,
    crypto,
    imagestyles,
    main as app_main,
    mcp_server,
    notion_db,
    notion_publish,
    notion_webhook,
    oidc,
    tenant,
    usage,
    web,
)


class TestCryptoModule(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"DATA_DIR": tmp, "ENCRYPTION_KEY": "master-test-key", "SESSION_SECRET": ""}):
            ct = crypto.encrypt_secret("wx_secret_hello_世界")
            self.assertTrue(ct.startswith("enc:v1:"))
            self.assertNotIn("wx_secret", ct)
            self.assertEqual(crypto.encrypt_secret(ct), ct)
            self.assertEqual(crypto.decrypt_secret(ct), "wx_secret_hello_世界")

            # 篡改密文 MAC 校验失败返回空
            bad = ct[:-2] + ("AA" if not ct.endswith("AA") else "BB")
            self.assertEqual(crypto.decrypt_secret(bad), "")
            # 老明文兼容原样返回
            self.assertEqual(crypto.decrypt_secret("legacy-plain"), "legacy-plain")


class TestMultiTenantEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp_dir = Path(cls._tmp.name)
        cls.db_path = cls.tmp_dir / "articles.db"
        cls.cfg_path = cls.tmp_dir / "config.json"
        cls.upload_dir = cls.tmp_dir / "uploads"
        cls.upload_dir.mkdir(parents=True, exist_ok=True)

        cls.patches = [
            patch.dict(os.environ, {"DATA_DIR": str(cls.tmp_dir), "ENCRYPTION_KEY": "saas-master-secret", "BASE_URL": "https://pub.test"}),
            patch.object(config, "CONFIG_PATH", cls.cfg_path),
            patch.object(tenant, "DB_PATH", cls.db_path),
            patch.object(article, "DB_PATH", cls.db_path),
            patch.object(auth, "_TOKEN_DB_PATH", cls.db_path),
            patch.object(auth, "AUTH_TOKEN", "admin-auth-tok"),
            patch.object(app_main, "AUTH_TOKEN", "admin-auth-tok"),
            patch.object(usage, "DB_PATH", cls.db_path),
            patch.object(imagestyles, "DB_PATH", lambda: cls.db_path),
            patch.object(notion_db, "CACHE_PATH", cls.tmp_dir / "notion_db_cache.json"),
            patch.object(notion_publish, "UPLOAD_DIR", cls.upload_dir),
            patch.object(app_main, "UPLOAD_DIR", cls.upload_dir),
            patch.object(mcp_server, "UPLOAD_DIR", cls.upload_dir),
            patch.object(oidc, "SESSION_SECRET", "unit-session-secret"),
        ]
        for p in cls.patches:
            p.start()

        tenant._wechat_pool.clear()
        notion_db.clear_cache()
        cls.client_ctx = TestClient(app_main.app)
        cls.client = cls.client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_ctx.__exit__(None, None, None)
        tenant._wechat_pool.clear()
        notion_db.clear_cache()
        for p in reversed(cls.patches):
            p.stop()
        cls._tmp.cleanup()

    def setUp(self):
        # 清理非 owner 测试数据与配置文件，保持用例间独立
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

    def _register_and_login(self, username: str, password: str = "pass123456") -> tuple[str, dict[str, str]]:
        c = TestClient(app_main.app)
        r = c.post(
            "/register",
            data={
                "username": username,
                "display_name": username.upper(),
                "password": password,
                "confirm_password": password,
            },
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/onboarding?welcome=1")
        cookie = r.cookies.get(oidc.SESSION_COOKIE)
        self.assertIsNotNone(cookie)
        uid = oidc.read_session(cookie)["uid"]
        return uid, {oidc.SESSION_COOKIE: cookie}

    def _admin_cookies(self) -> dict[str, str]:
        tenant.bootstrap_owner()
        return {
            oidc.SESSION_COOKIE: oidc.create_session(
                {"id": "owner", "username": "admin", "role": "admin", "name": "管理员", "auth_method": "oidc"}
            )
        }

    def test_seed_generation_reset_and_tenant_isolation(self):
        uid_a, ck_a = self._register_and_login("alice")
        uid_b, ck_b = self._register_and_login("bob")

        # 刚注册未配微信：没有 mcp_token / webhook_token
        ua0 = tenant.get_user_by_id(uid_a)
        self.assertEqual(ua0["mcp_token"], "")
        self.assertEqual(ua0["webhook_token"], "")
        self.assertIn("待配置", self.client.get("/settings", cookies=ck_a).text)

        # 配置微信 AppID + AppSecret -> 必须通过微信连通性校验，完成后自动生成种子并派生专属链接
        with patch.object(web.WechatAPI, "get_access_token", AsyncMock(return_value="tok_a")):
            r_save = self.client.post(
                "/settings",
                data={
                    "theme": "lapis",
                    "wx_appid": "wx_alice_01",
                    "wx_appsecret": "sec_alice_01",
                    "wx_author": "Alice",
                },
                cookies=ck_a,
                follow_redirects=False,
            )
        self.assertEqual(r_save.status_code, 303)
        self.assertIn("seed_created=1", r_save.headers["location"])

        ua1 = tenant.get_user_by_id(uid_a)
        old_mcp = ua1["mcp_token"]
        old_wh = ua1["webhook_token"]
        self.assertTrue(old_mcp.startswith("mcp_"))
        self.assertTrue(old_wh.startswith("wh_"))

        # 重置种子后，旧 token 立即作废，新 token 生效
        r_reset = self.client.post("/settings/reset-seed", cookies=ck_a)
        self.assertEqual(r_reset.status_code, 200)
        self.assertTrue(r_reset.json()["ok"])
        ua2 = tenant.get_user_by_id(uid_a)
        new_mcp = ua2["mcp_token"]
        new_wh = ua2["webhook_token"]
        self.assertNotEqual(old_mcp, new_mcp)
        self.assertNotEqual(old_wh, new_wh)

        # 旧 MCP URL 返回 401
        mcp_payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        mcp_headers = {"Accept": "application/json, text/event-stream"}
        r_old_mcp = self.client.post(f"/mcp/{old_mcp}", json=mcp_payload, headers=mcp_headers)
        self.assertEqual(r_old_mcp.status_code, 401)

        # 新 MCP URL 创建文章属于 Alice，Bob 不可见也不可预览/更新
        r_create = self.client.post(
            f"/mcp/{new_mcp}",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "create_article", "arguments": {"title": "Alice秘密文章", "content": "正文"}},
            },
            headers=mcp_headers,
        )
        self.assertEqual(r_create.status_code, 200)
        alice_arts = article.list_articles(user_id=uid_a)
        self.assertEqual(len(alice_arts), 1)
        art_id = alice_arts[0].id
        self.assertEqual(len(article.list_articles(user_id=uid_b)), 0)

        # Bob 登录后尝试预览或修改 Alice 文章 -> 403 / 404
        self.assertEqual(self.client.get(f"/preview/{art_id}", cookies=ck_b).status_code, 403)
        self.assertEqual(
            self.client.put(f"/api/articles/{art_id}", json={"title": "篡改"}, cookies=ck_b).status_code,
            404,
        )

        # 旧 Webhook 失效 404，新 Webhook 接收 verification_token 自动加密保存到 Alice 配置
        self.assertEqual(self.client.post(f"/notion/webhook/{old_wh}", json={"verification_token": "vt1"}).status_code, 404)
        r_wh = self.client.post(f"/notion/webhook/{new_wh}", json={"verification_token": "alice_wh_sec"})
        self.assertEqual(r_wh.status_code, 200)
        self.assertEqual(tenant.get_tenant_config(uid_a)["notion"]["webhook_secret"], "alice_wh_sec")

    def test_admin_permissions_and_user_disable_enforcement(self):
        uid_a, ck_a = self._register_and_login("charlie")
        with patch.object(web.WechatAPI, "get_access_token", AsyncMock(return_value="tok_c")):
            self.client.post(
                "/settings",
                data={"theme": "default", "wx_appid": "wx_c", "wx_appsecret": "sec_c"},
                cookies=ck_a,
                follow_redirects=False,
            )
        uc = tenant.get_user_by_id(uid_a)

        # 普通用户访问 /admin -> 403
        self.assertEqual(self.client.get("/admin", cookies=ck_a).status_code, 403)
        self.assertEqual(self.client.post("/settings/test-ai", cookies=ck_a).status_code, 403)

        # 管理员访问 /admin -> 200，可看到 charlie
        admin_ck = self._admin_cookies()
        admin_html = self.client.get("/admin", cookies=admin_ck).text
        self.assertIn("charlie", admin_html)

        # 管理员停用 charlie
        r_dis = self.client.post(
            f"/admin/users/{uid_a}/status",
            data={"status": "disabled"},
            cookies=admin_ck,
            follow_redirects=False,
        )
        self.assertEqual(r_dis.status_code, 303)

        # 停用后：Web 会话被踢回 /login、密码登录提示停用、MCP 和 Webhook 立即返回 403
        self.assertEqual(self.client.get("/", cookies=ck_a, follow_redirects=False).status_code, 303)
        r_login = self.client.post(
            "/login/password",
            data={"username": "charlie", "password": "pass123456"},
            follow_redirects=False,
        )
        self.assertIn("pw_err=disabled_user", r_login.headers["location"])

        self.assertEqual(
            self.client.post(f"/mcp/{uc['mcp_token']}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(f"/notion/webhook/{uc['webhook_token']}", json={"verification_token": "x"}).status_code,
            403,
        )

        # 管理员恢复启用 -> 立即恢复正常
        self.client.post(
            f"/admin/users/{uid_a}/status",
            data={"status": "active"},
            cookies=admin_ck,
            follow_redirects=False,
        )
        self.assertEqual(self.client.get("/", cookies=ck_a, follow_redirects=False).status_code, 200)

    def test_preview_image_mirror_and_wx_proxy_ssrf_guard(self):
        uid_a, ck_a = self._register_and_login("diana")
        fake_wx = MagicMock()
        fake_wx.upload_image = AsyncMock(return_value="https://mmbiz.qpic.cn/mmbiz_jpg/wx123/0?wx_fmt=jpeg")

        with patch.object(notion_publish, "_download_image", AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfakeimg")):
            wx_md, preview_md, _ = asyncio.run(
                notion_publish._transfer_images(
                    "段落\n\n![图](https://prod-files-secure.s3.us-west-2.amazonaws.com/abc/pic.png)\n",
                    fake_wx,
                    user_id=uid_a,
                    base_url="https://pub.test",
                    upload_dir=self.upload_dir,
                )
            )
        self.assertIn("https://mmbiz.qpic.cn/mmbiz_jpg/wx123/0", wx_md)
        self.assertIn(f"https://pub.test/images/{uid_a}/pub-", preview_md)

        # 验证写入的本地预览镜像确实可通过 /images/{uid}/{fname} 读取
        img_path = preview_md.split("](")[1].split(")")[0].replace("https://pub.test", "")
        r_img = self.client.get(img_path)
        self.assertEqual(r_img.status_code, 200)
        self.assertTrue(r_img.content.startswith(b"\x89PNG"))

        # 验证老文章包含微信图床链接时，预览页会重写为 /api/preview-img 代理且 <head> 含 no-referrer
        old_art = article.create_article(
            "老文章",
            "![微信图](https://mmbiz.qpic.cn/mmbiz_jpg/old123/0)",
            "<p>html</p>",
            user_id=uid_a,
        )
        r_prev = self.client.get(f"/preview/{old_art.id}", cookies=ck_a)
        self.assertEqual(r_prev.status_code, 200)
        self.assertIn('name="referrer" content="no-referrer"', r_prev.text)
        self.assertIn("/api/preview-img?url=https%3A%2F%2Fmmbiz.qpic.cn", r_prev.text)

        # /api/preview-img 严格拒绝非微信域名与内网 URL（防 SSRF）
        self.assertEqual(
            self.client.get("/api/preview-img?url=http://127.0.0.1:8000/secret", cookies=ck_a).status_code,
            400,
        )
        self.assertEqual(
            self.client.get("/api/preview-img?url=https://evil.example.com/a.jpg", cookies=ck_a).status_code,
            400,
        )

    def test_platform_ai_merge_and_free_notice(self):
        admin_ck = self._admin_cookies()
        self.client.post(
            "/admin/platform",
            data={
                "allow_registration": "on",
                "ai_base_url": "https://ai.platform.test/v1",
                "ai_api_key": "sk-platform-only-key",
                "ai_text_model": "gpt-text-p",
                "ai_image_model": "gpt-img-p",
            },
            cookies=admin_ck,
            follow_redirects=False,
        )
        uid, ck = self._register_and_login("eva")
        self.client.post(
            "/settings",
            data={
                "theme": "default",
                "ai_enabled": "on",
                "ai_style_mode": "fixed",
                "ai_style": "蓝白极简线稿",
                "ai_skip_if_has_images": "on",
                "ai_skip_min_images": "3",
            },
            cookies=ck,
            follow_redirects=False,
        )
        eff = tenant.get_effective_ai_config(uid)
        self.assertTrue(eff["enabled"])
        self.assertEqual(eff["api_key"], "sk-platform-only-key")
        self.assertEqual(eff["base_url"], "https://ai.platform.test/v1")
        self.assertEqual(eff["style"], "蓝白极简线稿")
        self.assertEqual(eff["skip_min_images"], 3)

        # 普通用户页面展示免费告知，且绝不暴露平台密钥或模型配置
        settings_html = self.client.get("/settings", cookies=ck).text
        self.assertIn("免费赠送", settings_html)
        self.assertNotIn("sk-platform-only-key", settings_html)
        self.assertNotIn("ai.platform.test", settings_html)


if __name__ == "__main__":
    unittest.main()
