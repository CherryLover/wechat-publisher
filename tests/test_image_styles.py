"""
配图风格单元测试（不预设风格池版）

风格由文生文模型按文章自由设计，这个模块只做：能不能用、和最近重复没有、记账。

覆盖：slug/校验/取名、历史记录与去重、resolve_style 的兜底分支、
_style_brief 的提示词内容、以及 build() 里「一套风格贯穿 4 张图」的集成行为。

跑法：python -m unittest discover -s tests -v
"""

import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import imagegen, imagestyles


GOOD_STYLE = (
    "dark techno editorial illustration on a deep navy background, neon cyan and violet "
    "accents, thin glowing geometric lines, faint grid, high contrast studio light"
)


class TempDataBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wpstyle_"))
        self._old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = str(self.tmp)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)


# --------------------------------------------------------------- 工具函数

class TestHelpers(unittest.TestCase):
    def test_slug_stable_and_distinct(self):
        a = imagestyles.slug(GOOD_STYLE)
        b = imagestyles.slug("  " + GOOD_STYLE + "  ")
        c = imagestyles.slug(GOOD_STYLE.replace("navy", "olive"))
        self.assertEqual(a, b)              # 只差空白 → 同一个
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("auto-"))

    def test_is_usable(self):
        self.assertFalse(imagestyles.is_usable(""))
        self.assertFalse(imagestyles.is_usable("beautiful picture"))       # 太短且没关键词
        self.assertFalse(imagestyles.is_usable("a" * 80))                  # 够长但没信息
        self.assertTrue(imagestyles.is_usable(GOOD_STYLE))

    def test_clean_name_keeps_given_name(self):
        self.assertEqual(imagestyles.clean_name("冷调蓝图", GOOD_STYLE), "冷调蓝图")

    def test_clean_name_derives_when_missing(self):
        name = imagestyles.clean_name("", GOOD_STYLE)
        self.assertTrue(name)
        self.assertLessEqual(len(name), 16)

    def test_clean_name_strips_whitespace_and_truncates(self):
        name = imagestyles.clean_name("  很 长 的 名 字 " * 5, GOOD_STYLE)
        self.assertNotIn(" ", name)
        self.assertLessEqual(len(name), 24)

    def test_normalize(self):
        self.assertEqual(imagestyles.normalize("  A   B \n"), "a b")


# --------------------------------------------------------------- 历史记录

class TestHistory(TempDataBase):
    def test_log_and_recent(self):
        imagestyles.log_use("a1", "auto-x", "冷调蓝图", GOOD_STYLE, "文章一")
        rows = imagestyles.recent_styles(4)
        self.assertEqual(rows[0]["style_id"], "auto-x")
        self.assertEqual(rows[0]["name"], "冷调蓝图")
        self.assertIn("navy", rows[0]["text"])
        self.assertEqual(rows[0]["title"], "文章一")

    def test_dedupe_by_style_id(self):
        imagestyles.log_use("a1", "auto-x", "甲", GOOD_STYLE)
        imagestyles.log_use("a2", "auto-x", "甲", GOOD_STYLE)
        imagestyles.log_use("a3", "auto-y", "乙", GOOD_STYLE + " extra grain")
        ids = imagestyles.recent_ids(4)
        self.assertEqual(ids, ["auto-y", "auto-x"])

    def test_limit(self):
        for i in range(6):
            imagestyles.log_use(f"a{i}", f"auto-{i}", f"风格{i}", GOOD_STYLE + f" v{i}")
        self.assertEqual(len(imagestyles.recent_styles(3)), 3)

    def test_repeated_detects_identical(self):
        imagestyles.log_use("a1", "auto-x", "甲", GOOD_STYLE)
        self.assertTrue(imagestyles.repeated(GOOD_STYLE))
        self.assertTrue(imagestyles.repeated("  " + GOOD_STYLE.upper() + " "))
        self.assertFalse(imagestyles.repeated(GOOD_STYLE + " with subtle paper grain"))

    def test_empty_repeated_is_false(self):
        self.assertFalse(imagestyles.repeated(""))

    def test_old_table_without_new_columns_still_works(self):
        """老库只有 style_id 列，升级后要能自动补列"""
        path = self.tmp / "articles.db"
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE ai_style_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            article_ref TEXT,
            article_title TEXT,
            style_id TEXT NOT NULL)""")
        conn.commit()
        conn.close()

        imagestyles.log_use("a1", "auto-x", "冷调蓝图", GOOD_STYLE, "文章一")
        rows = imagestyles.recent_styles(4)
        self.assertEqual(rows[0]["name"], "冷调蓝图")
        self.assertEqual(rows[0]["text"], GOOD_STYLE)

    def test_recent_log_has_display_fields(self):
        imagestyles.log_use("a1", "auto-x", "冷调蓝图", GOOD_STYLE, "文章一")
        row = imagestyles.recent_log(5)[0]
        self.assertEqual(row["name"], "冷调蓝图")
        self.assertEqual(row["ref"], "a1")


# ------------------------------------------------------------ 风格决议

class TestResolveStyle(TempDataBase):
    def test_free_style_is_used(self):
        chosen, fallback = imagegen.resolve_style(
            {"style": GOOD_STYLE, "style_name": "冷调科技"})
        self.assertFalse(fallback)
        self.assertEqual(chosen["name"], "冷调科技")
        self.assertEqual(chosen["desc_en"], GOOD_STYLE)
        self.assertEqual(chosen["id"], imagestyles.slug(GOOD_STYLE))

    def test_missing_style_falls_back(self):
        chosen, fallback = imagegen.resolve_style({})
        self.assertTrue(fallback)
        self.assertEqual(chosen["desc_en"], imagestyles.DEFAULT_STYLE)

    def test_short_style_falls_back(self):
        chosen, fallback = imagegen.resolve_style({"style": "nice"})
        self.assertTrue(fallback)

    def test_repeated_style_falls_back(self):
        imagestyles.log_use("a1", imagestyles.slug(GOOD_STYLE), "冷调科技", GOOD_STYLE, "旧文")
        chosen, fallback = imagegen.resolve_style({"style": GOOD_STYLE})
        self.assertTrue(fallback)
        self.assertEqual(chosen["name"], imagestyles.DEFAULT_NAME)

    def test_name_derived_when_absent(self):
        chosen, fallback = imagegen.resolve_style({"style": GOOD_STYLE})
        self.assertFalse(fallback)
        self.assertTrue(chosen["name"])

    def test_fixed_mode_wins(self):
        fixed = {"id": "custom", "name": "自定义", "desc_en": "my own style description here"}
        chosen, fallback = imagegen.resolve_style({"style": GOOD_STYLE}, fixed_style=fixed)
        self.assertEqual(chosen["id"], "custom")
        self.assertFalse(fallback)


# ------------------------------------------------------------ 提示词内容

class TestStyleBrief(TempDataBase):
    def test_auto_without_history_is_free(self):
        brief, fixed = imagegen._style_brief({"style_mode": "auto"})
        self.assertIsNone(fixed)
        self.assertIn("风格不预设", brief)
        self.assertIn("近期第一篇", brief)
        self.assertIn("不要出现任何文字", brief)

    def test_auto_lists_recent_styles(self):
        imagestyles.log_use("a1", "auto-x", "冷调科技", GOOD_STYLE, "旧文")
        brief, _ = imagegen._style_brief({"style_mode": "auto"})
        self.assertIn("冷调科技", brief)
        self.assertIn("换一套", brief)

    def test_fixed_mode_uses_custom_text(self):
        brief, fixed = imagegen._style_brief(
            {"style_mode": "fixed", "style": "深色科技感，霓虹青紫"})
        self.assertEqual(fixed["id"], "custom")
        self.assertIn("深色科技感", brief)

    def test_fixed_without_text_uses_base(self):
        _, fixed = imagegen._style_brief({"style_mode": "fixed", "style": ""})
        self.assertTrue(fixed["desc_en"])


# ------------------------------------------------------------ 提示词拼接

class TestApplyStyle(unittest.TestCase):
    def test_appends_style_and_no_text(self):
        out = imagegen.apply_style("a cat on a desk", "flat vector style")
        self.assertIn("flat vector style", out)
        self.assertIn("no text", out)

    def test_does_not_duplicate(self):
        first = imagegen.apply_style("a cat", "flat vector style")
        second = imagegen.apply_style(first, "flat vector style")
        self.assertEqual(first, second)

    def test_empty_prompt(self):
        self.assertIn("flat vector style", imagegen.apply_style("", "flat vector style"))

    def test_empty_style_keeps_prompt(self):
        self.assertEqual(imagegen.apply_style("a cat", ""), "a cat")


# ------------------------------------------------------------ build 集成

def _jpeg_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (240, 240, 240)).save(buf, "JPEG")
    return buf.getvalue()


class TestBuildIntegration(TempDataBase):
    def setUp(self):
        super().setUp()
        self.upload = self.tmp / "uploads"
        self.upload.mkdir()
        self.cfg = {
            "enabled": True, "base_url": "http://x", "api_key": "k",
            "text_model": "t", "image_model": "i", "image_api": "auto",
            "style_mode": "auto", "style": "",
            "max_width": 1200, "cover_size": "1024x1024",
            "illustration_size": "1024x1024",
        }
        self.captured = []

    def plan(self, style=GOOD_STYLE, name="冷调科技"):
        return {
            "style_name": name,
            "style": style,
            "cover": {"prompt": "cover scene", "alt": "封面"},
            "illustrations": [
                {"heading": "第一节", "after_paragraph": 1, "prompt": "ill one", "alt": "图一"},
                {"heading": "第二节", "after_paragraph": 1, "prompt": "ill two", "alt": "图二"},
                {"heading": "第三节", "after_paragraph": 1, "prompt": "ill three", "alt": "图三"},
            ],
        }

    async def _build(self, ref, markdown, plan=None):
        async def fake_plan(*a, **k):
            return plan or self.plan()

        async def fake_gen(cfg, prompt, size, **k):
            self.captured.append(prompt)
            return _jpeg_bytes()

        with mock.patch.object(imagegen, "get_cfg", lambda: self.cfg), \
             mock.patch.object(imagegen, "plan_prompts", fake_plan), \
             mock.patch.object(imagegen, "generate_image", fake_gen):
            return await imagegen.build("标题", markdown, ref=ref,
                                        upload_dir=self.upload, base="http://local")

    def run_async(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_all_four_prompts_share_the_free_style(self):
        md = "## 第一节\n\n段落\n\n## 第二节\n\n段落\n\n## 第三节\n\n段落\n"
        res = self.run_async(self._build("article-1", md))
        self.assertEqual(len(self.captured), 4)
        for p in self.captured:
            self.assertIn(GOOD_STYLE, p)
            self.assertIn("no text", p)
        self.assertEqual(res.style_name, "冷调科技")
        self.assertEqual(res.style_id, imagestyles.slug(GOOD_STYLE))

    def test_style_is_logged(self):
        md = "## 第一节\n\n段落\n\n## 第二节\n\n段落\n\n## 第三节\n\n段落\n"
        self.run_async(self._build("article-2", md))
        recent = imagestyles.recent_styles(4)
        self.assertEqual(recent[0]["name"], "冷调科技")
        self.assertIn("navy", recent[0]["text"])

    def test_fallback_style_when_model_gives_nothing(self):
        md = "## 第一节\n\n段落\n\n## 第二节\n\n段落\n\n## 第三节\n\n段落\n"
        plan = self.plan()
        plan.pop("style")
        res = self.run_async(self._build("article-3", md, plan=plan))
        self.assertEqual(res.style_name, imagestyles.DEFAULT_NAME)
        for p in self.captured:
            self.assertIn(imagestyles.DEFAULT_STYLE, p)

    def test_repeated_style_gets_rotated(self):
        md = "## 第一节\n\n段落\n\n## 第二节\n\n段落\n\n## 第三节\n\n段落\n"
        self.run_async(self._build("article-4", md))
        self.captured.clear()
        res = self.run_async(self._build("article-5", md))
        self.assertEqual(res.style_name, imagestyles.DEFAULT_NAME)   # 与上一篇重复 → 兜底
        self.assertNotEqual(self.captured[0], "")

    def test_existing_files_are_reused(self):
        md = "## 第一节\n\n段落\n\n## 第二节\n\n段落\n\n## 第三节\n\n段落\n"
        self.run_async(self._build("article-6", md))
        self.captured.clear()
        res2 = self.run_async(self._build("article-6", md))
        self.assertEqual(self.captured, [])
        self.assertTrue(res2.cover_bytes)

    def test_different_articles_can_get_different_styles(self):
        md = "## 第一节\n\n段落\n\n## 第二节\n\n段落\n\n## 第三节\n\n段落\n"
        other = ("soft watercolor illustration on textured paper, translucent washes, "
                 "muted palette, gentle bleeding edges, calm morning light")
        self.run_async(self._build("a-1", md))
        self.captured.clear()
        self.run_async(self._build("a-2", md, plan=self.plan(style=other, name="晨光水彩")))
        self.assertIn("watercolor", self.captured[0])
        self.assertNotIn("navy", self.captured[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
