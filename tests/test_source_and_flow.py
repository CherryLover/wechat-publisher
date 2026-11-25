"""Notion Database 数据源缓存与流程拓扑页测试"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import config, notion_db, oidc, web
from src.notion_source import NotionSource


SAMPLE_DB = {
    "id": "db-1111-2222",
    "title": [{"plain_text": "公众号文章库"}],
    "url": "https://www.notion.so/db11112222",
    "created_time": "2026-01-01T00:00:00.000Z",
    "last_edited_time": "2026-11-24T12:00:00.000Z",
    "properties": {
        "标题": {"type": "title"},
        "状态": {"type": "select"},
        "迁移备注": {"type": "rich_text"},
    },
}

SAMPLE_PAGES = [
    {
        "id": "aaaa-1111",
        "url": "https://www.notion.so/aaaa1111",
        "created_time": "2026-11-20T08:00:00.000Z",
        "last_edited_time": "2026-11-24T10:30:00.000Z",
        "cover": {"type": "external", "external": {"url": "https://example.com/c.jpg"}},
        "properties": {
            "标题": {"type": "title", "title": [{"plain_text": "第一篇技术文章"}]},
            "状态": {"type": "select", "select": {"name": "已发布"}},
        },
    },
    {
        "id": "bbbb-2222",
        "url": "https://www.notion.so/bbbb2222",
        "created_time": "2026-11-21T08:00:00.000Z",
        "last_edited_time": "2026-11-23T09:00:00.000Z",
        "cover": None,
        "properties": {
            "标题": {"type": "title", "title": [{"plain_text": "第二篇草稿"}]},
            "状态": {"type": "select", "select": {"name": "草稿"}},
        },
    },
]


class TestNotionDbCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_file = Path(self._tmp.name) / "notion_db_cache.json"
        self.p_cache = patch.object(notion_db, "CACHE_PATH", self.cache_file)
        self.p_cache.start()
        notion_db.clear_cache()

    def tearDown(self):
        notion_db.clear_cache()
        self.p_cache.stop()
        self._tmp.cleanup()

    def test_unconfigured_returns_gracefully(self):
        with patch.dict(os.environ, {"NOTION_TOKEN": "", "NOTION_DB_ID": "", "NOTION_DATABASE_ID": ""}):
            snap = asyncio.run(notion_db.get_database_snapshot())
        self.assertFalse(snap["configured"])
        self.assertIn("NOTION_TOKEN", snap["error"])

    def test_memory_and_disk_cache_and_force_refresh_and_stale_fallback(self):
        mock_get_db = AsyncMock(return_value=SAMPLE_DB)
        mock_query = AsyncMock(return_value=(SAMPLE_PAGES, False))
        env = {"NOTION_TOKEN": "secret_tok", "NOTION_DB_ID": "db-1111-2222"}

        with (
            patch.dict(os.environ, env),
            patch.object(NotionSource, "get_database", mock_get_db),
            patch.object(NotionSource, "query_database", mock_query),
        ):
            # 1) 首次回源，落盘
            s1 = asyncio.run(notion_db.get_database_snapshot(ttl=300))
            self.assertTrue(s1["configured"])
            self.assertFalse(s1["cache"]["hit"])
            self.assertEqual(s1["database"]["title"], "公众号文章库")
            self.assertEqual(s1["stats"]["total"], 2)
            self.assertEqual(s1["stats"]["status_counts"]["已发布"], 1)
            self.assertEqual(mock_get_db.call_count, 1)
            self.assertTrue(self.cache_file.exists())

            # 2) TTL 内再次调用 -> 命中内存缓存
            s2 = asyncio.run(notion_db.get_database_snapshot(ttl=300))
            self.assertTrue(s2["cache"]["hit"])
            self.assertEqual(mock_get_db.call_count, 1)

            # 3) 清内存模拟重启 -> 命中磁盘缓存
            notion_db.clear_cache()
            s3 = asyncio.run(notion_db.get_database_snapshot(ttl=300))
            self.assertTrue(s3["cache"]["hit"])
            self.assertEqual(mock_get_db.call_count, 1)

            # 4) 强制刷新 -> 重新打 API
            s4 = asyncio.run(notion_db.get_database_snapshot(force_refresh=True, ttl=300))
            self.assertFalse(s4["cache"]["hit"])
            self.assertEqual(mock_get_db.call_count, 2)

        # 5) API 异常时降级返回旧缓存
        with (
            patch.dict(os.environ, env),
            patch.object(NotionSource, "get_database", AsyncMock(side_effect=RuntimeError("timeout"))),
        ):
            s5 = asyncio.run(notion_db.get_database_snapshot(force_refresh=True, ttl=300))
            self.assertTrue(s5["cache"]["stale"])
            self.assertIn("timeout", s5["error"])
            self.assertEqual(s5["stats"]["total"], 2)


class TestSourceAndFlowRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self._tmp.name) / "config.json"
        self.cache_file = Path(self._tmp.name) / "notion_db_cache.json"
        self.p_cfg = patch.object(config, "CONFIG_PATH", self.cfg_path)
        self.p_cache = patch.object(notion_db, "CACHE_PATH", self.cache_file)
        self.p_secret = patch.object(oidc, "SESSION_SECRET", "unit-test-secret")
        self.p_articles = patch.object(web.article, "list_articles", return_value=[])
        self.p_src_map = patch.object(
            web.article,
            "list_notion_source_map",
            return_value={"aaaa1111": {"id": "local01", "updated_at": "2026-11-24T11:00:00", "published_at": "2026-11-24T11:00:00"}},
        )
        self.p_preview = patch.object(web, "create_preview_token", return_value={"token": "tok123"})
        self.p_usage = patch.object(
            web.usage, "totals", return_value={"calls": 4, "images": 4, "total_tokens": 1200, "prompt_tokens": 800, "completion_tokens": 400, "errors": 0}
        )
        for p in (self.p_cfg, self.p_cache, self.p_secret, self.p_articles, self.p_src_map, self.p_preview, self.p_usage):
            p.start()
        notion_db.clear_cache()

        app = FastAPI()
        app.include_router(web.router)
        self.client = TestClient(app)

    def tearDown(self):
        notion_db.clear_cache()
        for p in (self.p_usage, self.p_preview, self.p_src_map, self.p_articles, self.p_secret, self.p_cache, self.p_cfg):
            p.stop()
        self._tmp.cleanup()

    def _auth_cookies(self) -> dict[str, str]:
        return {oidc.SESSION_COOKIE: oidc.create_session({"sub": "u1", "name": "Owner", "auth_method": "oidc"})}

    def test_requires_login(self):
        self.assertEqual(self.client.get("/source", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get("/flow", follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.post("/source/refresh").status_code, 401)

    def test_source_and_flow_pages_render(self):
        env = {"NOTION_TOKEN": "secret_tok", "NOTION_DB_ID": "db-1111-2222"}
        with (
            patch.dict(os.environ, env),
            patch.object(NotionSource, "get_database", AsyncMock(return_value=SAMPLE_DB)),
            patch.object(NotionSource, "query_database", AsyncMock(return_value=(SAMPLE_PAGES, False))),
        ):
            r_src = self.client.get("/source", cookies=self._auth_cookies())
            self.assertEqual(r_src.status_code, 200)
            self.assertIn("公众号文章库", r_src.text)
            self.assertIn("第一篇技术文章", r_src.text)
            self.assertIn("第二篇草稿", r_src.text)
            self.assertIn("/preview/local01?token=tok123", r_src.text)
            self.assertIn("/notion/webhook", r_src.text)

            r_ref = self.client.post("/source/refresh", cookies=self._auth_cookies())
            self.assertEqual(r_ref.status_code, 200)
            self.assertTrue(r_ref.json()["ok"])
            self.assertEqual(r_ref.json()["total"], 2)

            r_flow = self.client.get("/flow", cookies=self._auth_cookies())
            self.assertEqual(r_flow.status_code, 200)
            self.assertIn("发布引擎", r_flow.text)
            self.assertIn("公众号文章库", r_flow.text)
            self.assertIn("微信公众号草稿箱", r_flow.text)
            self.assertIn("Notion Webhook", r_flow.text)
            self.assertIn("topo-svg", r_flow.text)


if __name__ == "__main__":
    unittest.main()
