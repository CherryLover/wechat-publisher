"""
配图风格池单元测试

覆盖：风格数据本身、白名单过滤、兜底轮换、固定模式、历史记录避重、
提示词拼接，以及 build() 里「一条风格贯穿 4 张图」的集成行为。

跑法：python -m unittest discover -s tests -v
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import imagegen, imagestyles


class StyleTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wpstyle_"))
        self._old_data = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = str(self.tmp)

    def tearDown(self):
        if self._old_data is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = self._old_data
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestStylePool(unittest.TestCase):
    def test_ids_unique(self):
        ids = [s["id"] for s in imagestyles.STYLES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_styles_have_required_fields(self):
        for s in imagestyles.STYLES:
            self.assertTrue(s["name"])
            self.assertTrue(s["mood"])
            self.assertEqual(len(s["colors"]), 3, s["id"])
            for c in s["colors"]:
                self.assertRegex(c, r"^#[0-9a-fA-F]{6}$")
            self.assertGreater(len(s["desc_en"]), 40, s["id"])
            self.assertNotIn("文字", s["desc_en"])

    def test_pool_size_reasonable(self):
        self.assertGreaterEqual(len(imagestyles.STYLES), 6)

    def test_catalog_lists_every_id(self):
        cat = imagestyles.prompt_catalog(imagestyles.STYLES)
        for s in imagestyles.STYLES:
            self.assertIn(s["id"], cat)
            self.assertIn(s["name"], cat)

    def test_get(self):
        self.assertEqual(imagestyles.get("blueprint")["id"], "blueprint")
        self.assertIsNone(imagestyles.get("不存在"))


class TestAllowed(unittest.TestCase):
    def test_empty_means_all(self):
        self.assertEqual(len(imagestyles.allowed([])), len(imagestyles.STYLES))
        self.assertEqual(len(imagestyles.allowed(None)), len(imagestyles.STYLES))

    def test_subset(self):
        pool = imagestyles.allowed(["blueprint", "watercolor"])
        self.assertEqual({s["id"] for s in pool}, {"blueprint", "watercolor"})

    def test_invalid_only_falls_back_to_all(self):
        self.assertEqual(len(imagestyles.allowed(["nope", "???])"])),
                         len(imagestyles.STYLES))

    def test_mixed_keeps_valid(self):
        pool = imagestyles.allowed(["blueprint", "nope"])
        self.assertEqual([s["id"] for s in pool], ["blueprint"])

    def test_order_follows_pool_definition(self):
        pool = imagestyles.allowed(["watercolor", "warm_flat"])
        ids = [s["id"] for s in pool]
        expected = [i for i in imagestyles.ALL_IDS if i in ids]
        self.assertEqual(ids, expected)


class TestPick(unittest.TestCase):
    def test_same_ref_is_stable(self):
        a = imagestyles.pick("article-abc")
        b = imagestyles.pick("article-abc")
        self.assertEqual(a["id"], b["id"])

    def test_different_refs_vary(self):
        ids = {imagestyles.pick(f"ref-{i}")["id"] for i in range(30)}
        self.assertGreaterEqual(len(ids), 3)

    def test_respects_allow(self):
        pool = ["tech_dark", "iso3d"]
        ids = {imagestyles.pick(f"ref-{i}", allow=pool)["id"] for i in range(30)}
        self.assertTrue(ids <= set(pool))

    def test_avoids_recent(self):
        recent = [s["id"] for s in imagestyles.STYLES]
        last = recent.pop()          # 只留一个没用过
        picked = imagestyles.pick("whatever", recent=recent)
        self.assertEqual(picked["id"], last)

    def test_recent_covers_all_still_returns(self):
        recent = [s["id"] for s in imagestyles.STYLES]
        picked = imagestyles.pick("whatever", recent=recent)
        self.assertIn(picked["id"], imagestyles.ALL_IDS)


class TestResolveStyle(StyleTestBase):
    def cfg(self, **kw):
        base = {"style_mode": "auto", "style_allow": []}
        base.update(kw)
        return base

    def test_valid_candidate_is_used(self):
        chosen, fallback = imagegen.resolve_style(
            "blueprint", self.cfg(), "ref", imagestyles.STYLES)
        self.assertEqual(chosen["id"], "blueprint")
        self.assertFalse(fallback)

    def test_invalid_candidate_falls_back(self):
        chosen, fallback = imagegen.resolve_style(
            "不存在的风格", self.cfg(), "ref", imagestyles.STYLES)
        self.assertTrue(fallback)
        self.assertIn(chosen["id"], imagestyles.ALL_IDS)

    def test_candidate_outside_allow_list_falls_back(self):
        chosen, fallback = imagegen.resolve_style(
            "blueprint", self.cfg(style_allow=["watercolor"]), "ref",
            imagestyles.allowed(["watercolor"]))
        self.assertTrue(fallback)
        self.assertEqual(chosen["id"], "watercolor")

    def test_recent_candidate_is_rotated_out(self):
        imagestyles.log_use("r", "blueprint", "旧文章")
        chosen, fallback = imagegen.resolve_style(
            "blueprint", self.cfg(), "ref", imagestyles.STYLES)
        self.assertTrue(fallback)
        self.assertNotEqual(chosen["id"], "blueprint")

    def test_single_style_pool_always_returns_it(self):
        imagestyles.log_use("r", "watercolor", "旧文章")
        pool = imagestyles.allowed(["watercolor"])
        chosen, fallback = imagegen.resolve_style("watercolor", self.cfg(style_allow=["watercolor"]),
                                                  "ref", pool)
        self.assertEqual(chosen["id"], "watercolor")
        self.assertFalse(fallback)

    def test_fixed_mode_ignores_candidate(self):
        fixed = {"id": "custom", "name": "自定义", "desc_en": "my custom style"}
        chosen, fallback = imagegen.resolve_style(
            "blueprint", self.cfg(style_mode="fixed"), "ref", imagestyles.STYLES,
            fixed_style=fixed)
        self.assertEqual(chosen["id"], "custom")
        self.assertFalse(fallback)


class TestApplyStyle(unittest.TestCase):
    def test_appends_style_and_no_text(self):
        out = imagegen.apply_style("a cat on a desk", "flat vector style")
        self.assertIn("flat vector style", out)
        self.assertIn("no text", out)

    def test_does_not_duplicate(self):
        first = imagegen.apply_style("a cat", "flat vector style")
        second = imagegen.apply_style(first, "flat vector style")
        self.assertEqual(first, second)
        self.assertEqual(second.lower().count("flat vector style"), 1)

    def test_empty_prompt(self):
        out = imagegen.apply_style("", "flat vector style")
        self.assertIn("flat vector style", out)

    def test_empty_style_keeps_prompt(self):
        self.assertEqual(imagegen.apply_style("a cat", ""), "a cat")


class TestStyleHistory(StyleTestBase):
    def test_log_and_recent(self):
        imagestyles.log_use("a1", "blueprint", "文章一")
        imagestyles.log_use("a2", "watercolor", "文章二")
        self.assertEqual(imagestyles.recent_ids(4), ["watercolor", "blueprint"])

    def test_recent_dedupes(self):
        for _ in range(3):
            imagestyles.log_use("a1", "iso3d", "同一篇")
        self.assertEqual(imagestyles.recent_ids(4), ["iso3d"])

    def test_recent_limit(self):
        for sid in imagestyles.ALL_IDS:
            imagestyles.log_use("x", sid, "t")
        self.assertEqual(len(imagestyles.recent_ids(3)), 3)

    def test_recent_log_has_names(self):
        imagestyles.log_use("a1", "blueprint", "文章一")
        rows = imagestyles.recent_log(5)
        self.assertEqual(rows[0]["style_id"], "blueprint")
        self.assertEqual(rows[0]["name"], imagestyles.get("blueprint")["name"])


def _jpeg_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (240, 240, 240)).save(buf, "JPEG")
    return buf.getvalue()


class TestBuildIntegration(StyleTestBase):
    """不联网：把模型调用换成假的，验证风格贯穿与落盘"""

    def setUp(self):
        super().setUp()
        self.upload = self.tmp / "uploads"
        self.upload.mkdir()
        self.cfg = {
            "enabled": True, "base_url": "http://x", "api_key": "k",
            "text_model": "t", "image_model": "i", "image_api": "auto",
            "style_mode": "auto", "style_allow": [], "style": "",
            "max_width": 1200, "cover_size": "1024x1024",
            "illustration_size": "1024x1024",
        }

    def plan(self, style_id="blueprint"):
        return {
            "style_id": style_id,
            "style": "whatever the model said",
            "cover": {"prompt": "cover scene", "alt": "封面"},
            "illustrations": [
                {"heading": "第一节", "after_paragraph": 1, "prompt": "ill one", "alt": "图一"},
                {"heading": "第二节", "after_paragraph": 1, "prompt": "ill two", "alt": "图二"},
                {"heading": "第三节", "after_paragraph": 1, "prompt": "ill three", "alt": "图三"},
            ],
        }

    async def _build(self, ref, markdown):
        captured = []

        async def fake_plan(*a, **k):
            return self.plan()

        async def fake_gen(cfg, prompt, size, **k):
            captured.append(prompt)
            return _jpeg_bytes()

        with mock.patch.object(imagegen, "get_cfg", lambda: self.cfg), \
             mock.patch.object(imagegen, "plan_prompts", fake_plan), \
             mock.patch.object(imagegen, "generate_image", fake_gen):
            res = await imagegen.build("标题", markdown, ref=ref,
                                       upload_dir=self.upload, base="http://local")
        return res, captured

    def test_all_four_prompts_share_the_same_style(self):
        md = "## 第一节\n\n段落\n\n## 第二节\n\n段落\n\n## 第三节\n\n段落\n"
        res, captured = self._run(self._build("article-1", md))
        self.assertEqual(res.style_id, "blueprint")
        self.assertEqual(res.style_name, imagestyles.get("blueprint")["name"])
        self.assertEqual(len(captured), 4)                     # 封面 + 3 插图
        desc = imagestyles.get("blueprint")["desc_en"]
        for p in captured:
            self.assertIn(desc, p)
            self.assertIn("no text", p)

    def test_style_is_logged_and_avoided_next_time(self):
        md = "## 第一节\n\n段落\n"
        self._run(self._build("article-1", md))
        self.assertIn("blueprint", imagestyles.recent_ids(4))

    def test_existing_files_are_reused(self):
        md = "## 第一节\n\n段落\n"
        self._run(self._build("article-2", md))
        res2, captured2 = self._run(self._build("article-2", md))
        self.assertEqual(captured2, [])                        # 第二次一张都没重新生成
        self.assertTrue(res2.cover_bytes)

    # 小工具：同步跑 async
    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
