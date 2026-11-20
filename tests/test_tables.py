"""
表格转换单元测试

覆盖三块：
1. Notion table 块 → GFM Markdown（表头、补齐、转义、空表、单列…）
2. 裸竖线表格合并（Notion 里粘贴 markdown 表格被空行拆散的情况）
3. HTML 渲染与微信友好样式（内联样式、斑马纹、空表头、主题色）

跑法：python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.html_convert import (
    markdown_to_html,
    markdown_to_wechat_html,
    merge_pipe_tables,
    style_tables,
)
from src.notion_source import blocks_to_markdown, table_to_markdown


def rt(text, bold=False, code=False, link=None):
    """造一个 Notion rich_text 片段"""
    node = {
        "type": "text",
        "plain_text": text,
        "annotations": {"bold": bold, "italic": False, "code": code,
                        "strikethrough": False, "underline": False, "color": "default"},
        "text": {"content": text, "link": ({"url": link} if link else None)},
    }
    return node


def table_block(rows, width=None, header=True, row_header=False):
    """造一个 Notion table 块（含 _children）"""
    ncol = width if width is not None else (len(rows[0]) if rows else 0)
    return {
        "type": "table",
        "table": {"table_width": ncol, "has_column_header": header,
                  "has_row_header": row_header},
        "_children": [
            {"type": "table_row", "table_row": {"cells": [[rt(c)] for c in row]}}
            for row in rows
        ],
    }


# ---------------------------------------------------------------- Notion → MD

class TestNotionTableToMarkdown(unittest.TestCase):
    def test_with_column_header(self):
        lines = table_to_markdown(
            {"table": {"table_width": 3, "has_column_header": True}},
            [{"table_row": {"cells": [[rt("型号")], [rt("定位")], [rt("价格")]]}},
             {"table_row": {"cells": [[rt("Hydrogen")], [rt("日常")], [rt("1.5")]]}}],
        )
        self.assertEqual(lines[0], "| 型号 | 定位 | 价格 |")
        self.assertEqual(lines[1], "| --- | --- | --- |")
        self.assertEqual(lines[2], "| Hydrogen | 日常 | 1.5 |")
        self.assertEqual(lines[-1], "")

    def test_without_column_header_gets_empty_header(self):
        lines = table_to_markdown(
            {"table": {"table_width": 2, "has_column_header": False}},
            [{"table_row": {"cells": [[rt("a")], [rt("b")]]}},
             {"table_row": {"cells": [[rt("c")], [rt("d")]]}}],
        )
        self.assertEqual(lines[0], "|  |  |")
        self.assertEqual(lines[1], "| --- | --- |")
        self.assertEqual(lines[2], "| a | b |")
        self.assertEqual(lines[3], "| c | d |")

    def test_pipe_in_cell_is_escaped(self):
        lines = table_to_markdown(
            {"table": {"table_width": 2, "has_column_header": True}},
            [{"table_row": {"cells": [[rt("A|B")], [rt("x")]]}},
             {"table_row": {"cells": [[rt("1")], [rt("2")]]}}],
        )
        self.assertEqual(lines[0], "| A\\|B | x |")

    def test_newline_becomes_br(self):
        lines = table_to_markdown(
            {"table": {"table_width": 1, "has_column_header": True}},
            [{"table_row": {"cells": [[rt("第一行\n第二行")]]}}],
        )
        self.assertEqual(lines[0], "| 第一行<br>第二行 |")

    def test_ragged_rows_padded_to_width(self):
        lines = table_to_markdown(
            {"table": {"table_width": 3, "has_column_header": True}},
            [{"table_row": {"cells": [[rt("h1")], [rt("h2")], [rt("h3")]]}},
             {"table_row": {"cells": [[rt("only")]]}}],
        )
        self.assertEqual(lines[2], "| only |  |  |")

    def test_row_longer_than_width_truncated(self):
        lines = table_to_markdown(
            {"table": {"table_width": 2, "has_column_header": False}},
            [{"table_row": {"cells": [[rt("a")], [rt("b")], [rt("c")]]}}],
        )
        self.assertEqual(lines[2], "| a | b |")

    def test_empty_table_returns_nothing(self):
        self.assertEqual(table_to_markdown({"table": {"table_width": 3}}, []), [])

    def test_missing_table_width_falls_back_to_cell_count(self):
        lines = table_to_markdown(
            {"table": {"has_column_header": True}},
            [{"table_row": {"cells": [[rt("a")], [rt("b")]]}},
             {"table_row": {"cells": [[rt("1")], [rt("2")]]}}],
        )
        self.assertEqual(lines[1], "| --- | --- |")

    def test_single_column(self):
        lines = table_to_markdown(
            {"table": {"table_width": 1, "has_column_header": True}},
            [{"table_row": {"cells": [[rt("项目")]]}},
             {"table_row": {"cells": [[rt("值")]]}}],
        )
        self.assertEqual(lines[0], "| 项目 |")
        self.assertEqual(lines[1], "| --- |")

    def test_rich_text_marks_kept(self):
        lines = table_to_markdown(
            {"table": {"table_width": 3, "has_column_header": True}},
            [{"table_row": {"cells": [[rt("粗", bold=True)], [rt("码", code=True)],
                                     [rt("链", link="https://a.b")]]}},
             {"table_row": {"cells": [[rt("1")], [rt("2")], [rt("3")]]}}],
        )
        self.assertEqual(lines[0], "| **粗** | `码` | [链](https://a.b) |")

    def test_blocks_to_markdown_renders_table_block(self):
        md = blocks_to_markdown([table_block([["型号", "价格"], ["Sol", "1.5"]])])
        self.assertIn("| 型号 | 价格 |", md)
        self.assertIn("| --- | --- |", md)
        self.assertIn("| Sol | 1.5 |", md)

    def test_orphan_table_row_still_makes_valid_table(self):
        md = blocks_to_markdown([
            {"type": "table_row", "table_row": {"cells": [[rt("a")], [rt("b")]]}},
        ])
        self.assertIn("| a | b |", md)
        self.assertIn("| --- | --- |", md)


# ------------------------------------------------------------- 裸竖线表格合并

class TestMergePipeTables(unittest.TestCase):
    def test_rows_split_by_blank_lines_are_merged(self):
        md = "前言\n\n| a | b |\n\n| 1 | 2 |\n\n| 3 | 4 |\n\n后记"
        out = merge_pipe_tables(md)
        # 没有分隔行 → 首行当表头
        self.assertIn("| a | b |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |", out)
        self.assertIn("前言", out)
        self.assertIn("后记", out)

    def test_already_valid_table_unchanged(self):
        md = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        self.assertEqual(merge_pipe_tables(md),
                         "| a | b |\n| --- | --- |\n| 1 | 2 |\n")

    def test_code_fence_untouched(self):
        md = "```\n| a | b |\n| 1 | 2 |\n```"
        self.assertEqual(merge_pipe_tables(md), md)

    def test_single_pipe_line_is_not_made_into_table(self):
        md = "看这里\n| 只有一行 |\n结束了"
        out = merge_pipe_tables(md)
        self.assertNotIn("| --- |", out)

    def test_ragged_columns_padded(self):
        out = merge_pipe_tables("| a | b | c |\n| 1 | 2 |")
        self.assertIn("| 1 | 2 |  |", out)
        self.assertIn("| --- | --- | --- |", out)

    def test_alignment_separator_preserved(self):
        out = merge_pipe_tables("| a | b |\n| :-- | --: |\n| 1 | 2 |")
        self.assertIn("| :-- | --: |", out)

    def test_escaped_pipe_survives_merge(self):
        out = merge_pipe_tables("| a\\|b | c |\n| --- | --- |\n| 1 | 2 |")
        self.assertIn("a\\|b", out)
        self.assertEqual(out.count("| --- |"), 1)

    def test_text_with_pipe_in_middle_untouched(self):
        md = "a | b 只是普通文字\n| x | y |\n| 1 | 2 |"
        out = merge_pipe_tables(md)
        self.assertIn("a | b 只是普通文字", out)

    def test_blank_lines_inside_table_removed(self):
        out = merge_pipe_tables("| a | b |\n\n| 1 | 2 |")
        self.assertNotIn("\n\n| 1 | 2 |", out)

    def test_table_without_header_gets_synthetic_header(self):
        out = merge_pipe_tables("| 1 | 2 |\n| 3 | 4 |")
        self.assertIn("| 1 | 2 |\n| --- | --- |\n| 3 | 4 |", out)


# ------------------------------------------------------------------- HTML 侧

class TestTableHtml(unittest.TestCase):
    MD = ("| 型号 | 定位 | 价格 |\n"
          "| --- | --- | --- |\n"
          "| Hydrogen | 日常 | 1.5 |\n"
          "| Neon | 中间 | 3 |\n"
          "| Carbon | 最强 | 11 |\n")

    def test_markdown_table_parses_to_html(self):
        html = markdown_to_html(self.MD)
        self.assertIn("<table>", html)
        self.assertEqual(html.count("<th>"), 3)
        self.assertEqual(html.count("<tr>"), 4)

    def test_pipes_do_not_leak_as_text(self):
        html = markdown_to_html(self.MD)
        self.assertNotIn("| 型号", html)
        self.assertIn("Hydrogen", html)

    def test_style_tables_adds_inline_styles(self):
        html = style_tables(markdown_to_html(self.MD), "default")
        self.assertIn("<table", html)
        self.assertIn("border-collapse:collapse", html)
        self.assertIn("<th", html)
        self.assertIn("padding:8px 10px", html)
        self.assertIn("border:1px solid rgba(0,0,0,0.10)", html)
        self.assertIn('overflow-x:auto', html)

    def test_theme_color_used_for_header(self):
        html = style_tables(markdown_to_html(self.MD), "orangeheart")
        # orangeheart 主色 #e9694a → rgba(233,105,74,0.08)
        self.assertIn("rgba(233,105,74,0.08)", html)

    def test_unknown_theme_falls_back(self):
        html = style_tables(markdown_to_html(self.MD), "不存在的主题")
        self.assertIn("rgba(91,107,107,0.08)", html)

    def test_gradient_theme_falls_back(self):
        html = style_tables(markdown_to_html(self.MD), "rainbow")
        self.assertIn("rgba(91,107,107,0.08)", html)

    def test_zebra_on_even_data_rows(self):
        html = style_tables(markdown_to_html(self.MD), "default")
        zebra = "background:rgba(255,80,44,0.03)"
        rows = html.split("<tr>")
        # rows[1] 表头（只有表头底色），rows[2] 第一条数据（加斑马），rows[3] 不加
        self.assertNotIn(zebra, rows[1])
        self.assertIn(zebra, rows[2])
        self.assertNotIn(zebra, rows[3])
        self.assertIn(zebra, rows[4])

    def test_empty_thead_removed(self):
        md = "|  |  |\n| --- | --- |\n| a | b |\n"
        html = style_tables(markdown_to_html(md), "default")
        self.assertNotIn("<thead>", html)
        self.assertIn("<td", html)

    def test_keeps_alignment_from_markdown(self):
        md = "| a | b |\n| --- | ---: |\n| 1 | 2 |\n"
        html = style_tables(markdown_to_html(md), "default")
        self.assertIn("text-align: right", html)

    def test_no_table_returns_input_untouched(self):
        html = "<p>没有表格</p>"
        self.assertEqual(style_tables(html, "default"), html)

    def test_published_html_has_no_style_tag_for_tables(self):
        html = markdown_to_wechat_html(self.MD, "orangeheart")
        self.assertIn("<table", html)
        self.assertIn("border-collapse:collapse", html)
        self.assertNotIn("<table style=\"\">", html)

    def test_table_survives_list_prefix_pass(self):
        md = self.MD + "\n- 列表项一\n- 列表项二\n"
        html = markdown_to_wechat_html(md, "default")
        self.assertIn("<table", html)
        self.assertIn("• ", html.replace("• ", "• "))  # 列表前缀仍在
        self.assertEqual(html.count("<table"), 1)


# ------------------------------------------------- Notion → HTML 端到端

class TestEndToEnd(unittest.TestCase):
    """还原真实那篇文章里的表格：3 列 5 行（首行表头）"""

    ROWS = [
        ["型号", "定位", "输入 / 输出"],
        ["Hydrogen", "日常、快", "1.5 / 7.5"],
        ["Neon", "中间档", "3 / 15"],
        ["Carbon", "最强", "11 月 30 日前 2 / 10，12 月 1 日起 4 / 20"],
        ["Argon", "家族底座", "与 Carbon 相同"],
    ]

    def test_notion_table_to_published_html(self):
        md = blocks_to_markdown([table_block(self.ROWS)])
        html = markdown_to_wechat_html(md, "orangeheart")

        self.assertIn("<table", html)
        self.assertEqual(html.count("<th "), 3)      # 注意 <thead> 也以 <th 开头
        self.assertEqual(html.count("<td "), 12)     # 4 行 × 3 列
        self.assertIn("Hydrogen", html)
        self.assertIn("11 月 30 日前 2 / 10", html)
        # 裸竖线不该出现在正文里
        self.assertNotIn("| 型号", html)
        self.assertNotIn("| --- |", html)
        # 样式已内联
        self.assertIn("border-collapse:collapse", html)
        self.assertIn("rgba(233,105,74,0.08)", html)

    def test_preview_path_also_styles_table(self):
        from src.html_convert import apply_theme_for_preview
        md = blocks_to_markdown([table_block(self.ROWS)])
        html = apply_theme_for_preview(markdown_to_html(md), "default")
        self.assertIn("<table", html)
        self.assertIn("border-collapse:collapse", html)
        self.assertIn("<style>", html)     # 预览仍然带主题 CSS

    def test_merged_pipe_table_renders(self):
        """模拟 Notion 里粘贴 markdown 表格：每行被拆成独立段落"""
        md = ("| 型号 | 价格 |\n\n| Sol | 1.5 |\n\n| Astra | 15 |\n")
        merged = merge_pipe_tables(md)
        html = markdown_to_wechat_html(merged, "default")
        self.assertIn("<table", html)
        self.assertEqual(html.count("<th "), 2)     # 首行当表头
        self.assertEqual(html.count("<td "), 4)     # 2 行 × 2 列


# ------------------------------------------------------- 更多边界情况（补充）

class TestNotionTableMoreEdges(unittest.TestCase):
    def test_has_row_header_flag_does_not_break(self):
        lines = table_to_markdown(
            {"table": {"table_width": 2, "has_column_header": True, "has_row_header": True}},
            [{"table_row": {"cells": [[rt("型号")], [rt("价格")]]}},
             {"table_row": {"cells": [[rt("Sol")], [rt("1.5")]]}}],
        )
        self.assertEqual(lines[0], "| 型号 | 价格 |")
        self.assertEqual(lines[2], "| Sol | 1.5 |")

    def test_empty_cell(self):
        lines = table_to_markdown(
            {"table": {"table_width": 2, "has_column_header": True}},
            [{"table_row": {"cells": [[rt("a")], [rt("b")]]}},
             {"table_row": {"cells": [[rt("1")], [rt("")]]}}],
        )
        self.assertEqual(lines[2], "| 1 |  |")

    def test_cell_with_backslash_pipe(self):
        lines = table_to_markdown(
            {"table": {"table_width": 1, "has_column_header": True}},
            [{"table_row": {"cells": [[rt("a\\|b")]]}}],
        )
        # 反斜杠本身保留，竖线再加一层转义
        self.assertEqual(lines[0], "| a\\\\|b |")

    def test_header_only_table(self):
        lines = table_to_markdown(
            {"table": {"table_width": 2, "has_column_header": True}},
            [{"table_row": {"cells": [[rt("a")], [rt("b")]]}}],
        )
        self.assertEqual(len([x for x in lines if x]), 2)   # 表头 + 分隔行


class TestMergeMoreEdges(unittest.TestCase):
    def test_two_tables_separated_by_text_not_merged(self):
        md = "| a | b |\n| 1 | 2 |\n\n中间正文\n\n| c | d |\n| 3 | 4 |"
        out = merge_pipe_tables(md)
        self.assertEqual(out.count("| --- |"), 2)
        self.assertIn("中间正文", out)

    def test_table_at_document_start(self):
        out = merge_pipe_tables("| a | b |\n| --- | --- |\n| 1 | 2 |\n\n正文")
        self.assertTrue(out.startswith("| a | b |"))
        self.assertIn("正文", out)

    def test_table_at_document_end(self):
        out = merge_pipe_tables("正文\n\n| a | b |\n| --- | --- |\n| 1 | 2 |")
        self.assertTrue(out.rstrip().endswith("| 1 | 2 |"))

    def test_leading_spaces_tolerated(self):
        out = merge_pipe_tables("   | a | b |\n   | 1 | 2 |")
        self.assertIn("| --- | --- |", out)

    def test_five_columns(self):
        out = merge_pipe_tables("| a | b | c | d | e |\n| 1 | 2 | 3 | 4 | 5 |")
        self.assertEqual(out.count("---"), 5)


class TestTableHtmlMoreEdges(unittest.TestCase):
    MD2 = ("| a | b |\n| --- | --- |\n| 1 | 2 |\n")

    def test_two_tables_both_styled(self):
        html = style_tables(markdown_to_html(self.MD2 + "\n正文\n\n" + self.MD2), "default")
        self.assertEqual(html.count("<table"), 2)
        self.assertEqual(html.count("border-collapse:collapse"), 2)
        self.assertEqual(html.count('overflow-x:auto'), 2)

    def test_idempotent(self):
        once = style_tables(markdown_to_html(self.MD2), "default")
        twice = style_tables(once, "default")
        self.assertEqual(once, twice)
        self.assertEqual(twice.count('overflow-x:auto'), 1)

    def test_rich_text_inside_cell(self):
        md = "| a | b |\n| --- | --- |\n| **粗** | `码` |\n"
        html = style_tables(markdown_to_html(md), "default")
        self.assertIn("<strong>粗</strong>", html)
        self.assertIn("<code>码</code>", html)
        self.assertIn("<td", html)

    def test_five_column_table_renders(self):
        md = "| a | b | c | d | e |\n| --- | --- | --- | --- | --- |\n| 1 | 2 | 3 | 4 | 5 |\n"
        html = markdown_to_wechat_html(md, "default")
        self.assertEqual(html.count("<th "), 5)
        self.assertEqual(html.count("<td "), 5)

    def test_header_only_table_renders(self):
        md = "| a | b |\n| --- | --- |\n"
        html = markdown_to_wechat_html(md, "default")
        self.assertIn("<table", html)
        self.assertIn("a", html)

    def test_table_between_paragraphs_keeps_order(self):
        md = "前文\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n后文"
        html = markdown_to_wechat_html(md, "default")
        self.assertLess(html.index("前文"), html.index("<table"))
        self.assertLess(html.index("<table"), html.index("后文"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
