"""
Notion 数据源

职责：读取 Notion 页面（属性 + 正文块），转成 Markdown，更新状态。
只管读和转换，不负责发布。
"""

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"

# 发布时跳过这些块（迁移说明、提示等噪音）
SKIP_BLOCK_TYPES = {"callout", "table_of_contents", "breadcrumb", "child_page", "child_database"}


class NotionError(Exception):
    pass


class NotionSource:
    """Notion API 薄封装"""

    def __init__(self, token: str):
        if not token:
            raise NotionError("缺少 NOTION_TOKEN")
        self.token = token

    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = NOTION_BASE + path
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=self.headers, json=payload)
        if resp.status_code >= 400:
            raise NotionError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ---------- 读取 ----------

    async def get_page(self, page_id: str) -> dict:
        return await self._request("GET", f"/pages/{page_id}")

    async def get_children(self, block_id: str) -> list[dict]:
        """取某个块/页面下的全部子块（自动翻页）"""
        results, cursor = [], None
        while True:
            path = f"/blocks/{block_id}/children?page_size=100"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = await self._request("GET", path)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                return results
            cursor = data.get("next_cursor")

    async def get_block_tree(self, page_id: str, max_depth: int = 3) -> list[dict]:
        """递归取块树，便于处理嵌套列表"""
        blocks = await self.get_children(page_id)
        if max_depth <= 1:
            return blocks
        for b in blocks:
            if b.get("has_children"):
                try:
                    b["_children"] = await self.get_block_tree(b["id"], max_depth - 1)
                except NotionError as e:
                    logger.warning("读取子块失败 %s: %s", b.get("id"), e)
                    b["_children"] = []
        return blocks

    # ---------- 属性工具 ----------

    @staticmethod
    def prop_text(page: dict, name: str) -> Optional[str]:
        """取 title / rich_text 属性的纯文本"""
        prop = (page.get("properties") or {}).get(name)
        if not prop:
            return None
        t = prop.get("type")
        if t == "title":
            return "".join(x["plain_text"] for x in prop.get("title", []))
        if t == "rich_text":
            return "".join(x["plain_text"] for x in prop.get("rich_text", []))
        if t == "select":
            return (prop.get("select") or {}).get("name")
        if t == "number":
            return prop.get("number")
        if t == "url":
            return prop.get("url")
        if t == "date":
            return (prop.get("date") or {}).get("start")
        return None

    # ---------- 写回 ----------

    async def set_select(self, page_id: str, prop: str, value: Optional[str]) -> None:
        payload = {"properties": {prop: {"select": ({"name": value} if value else None)}}}
        await self._request("PATCH", f"/pages/{page_id}", payload)

    async def set_rich_text(self, page_id: str, prop: str, value: str) -> None:
        payload = {"properties": {prop: {"rich_text": [
            {"type": "text", "text": {"content": value[:1900]}}
        ]}}}
        await self._request("PATCH", f"/pages/{page_id}", payload)


# ---------- 块 → Markdown ----------

def _rich(rich_text: list) -> str:
    """把 Notion rich_text 数组还原成带简单标记的文本"""
    out = []
    for node in rich_text or []:
        text = node.get("plain_text", "")
        ann = node.get("annotations") or {}
        href = ((node.get("text") or {}).get("link") or {}).get("url") if node.get("text") else None
        if node.get("type") == "equation":
            out.append(node.get("equation", {}).get("expression", ""))
            continue
        if ann.get("code"):
            text = f"`{text}`"
        if ann.get("bold"):
            text = f"**{text}**"
        if ann.get("italic"):
            text = f"*{text}*"
        if href:
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def _image_url(block: dict) -> Optional[str]:
    img = block.get("image") or {}
    kind = img.get("type")
    if kind == "external":
        return (img.get("external") or {}).get("url")
    return (img.get("file") or {}).get("url")


def blocks_to_markdown(blocks: list[dict], depth: int = 0) -> str:
    """Notion 块数组 → Markdown"""
    lines: list[str] = []
    indent = "  " * depth

    for idx, b in enumerate(blocks):
        kind = b.get("type")
        nxt = blocks[idx + 1].get("type") if idx + 1 < len(blocks) else None
        if kind in SKIP_BLOCK_TYPES:
            continue
        data = b.get(kind) or {}

        if kind == "paragraph":
            text = _rich(data.get("rich_text"))
            if text.strip():
                lines.append(text)
                lines.append("")

        elif kind in ("heading_1", "heading_2", "heading_3"):
            level = int(kind[-1])
            text = _rich(data.get("rich_text"))
            if text.strip():
                lines.append("#" * level + " " + text)
                lines.append("")

        elif kind == "bulleted_list_item":
            lines.append(indent + "- " + _rich(data.get("rich_text")))
            if b.get("_children"):
                lines.append(blocks_to_markdown(b["_children"], depth + 1))
            if nxt != "bulleted_list_item":
                lines.append("")

        elif kind == "numbered_list_item":
            lines.append(indent + "1. " + _rich(data.get("rich_text")))
            if b.get("_children"):
                lines.append(blocks_to_markdown(b["_children"], depth + 1))
            if nxt != "numbered_list_item":
                lines.append("")

        elif kind == "to_do":
            mark = "x" if data.get("checked") else " "
            lines.append(indent + f"- [{mark}] " + _rich(data.get("rich_text")))
            if nxt != "to_do":
                lines.append("")

        elif kind == "quote":
            text = _rich(data.get("rich_text"))
            if text.strip():
                lines.append("> " + text)
                lines.append("")

        elif kind == "code":
            lang = data.get("language", "")
            lines.append(f"```{lang}")
            lines.append(_rich(data.get("rich_text")))
            lines.append("```")
            lines.append("")

        elif kind == "divider":
            lines.append("---")
            lines.append("")

        elif kind == "image":
            url = _image_url(b)
            if url:
                caption = _rich(data.get("caption")).strip() or "图片"
                lines.append(f"![{caption}]({url})")
                lines.append("")

        elif kind == "table_row":
            cells = data.get("cells") or []
            lines.append("| " + " | ".join(_rich(c) for c in cells) + " |")
            if nxt != "table_row":
                lines.append("")

        elif kind == "toggle":
            text = _rich(data.get("rich_text"))
            if text.strip():
                lines.append(text)
                lines.append("")
            if b.get("_children"):
                lines.append(blocks_to_markdown(b["_children"], depth))

        elif b.get("_children"):
            lines.append(blocks_to_markdown(b["_children"], depth))

    return "\n".join(lines)


async def page_to_markdown(source: NotionSource, page_id: str) -> tuple[str, str]:
    """返回 (标题, 正文 Markdown)"""
    page = await source.get_page(page_id)
    title = NotionSource.prop_text(page, "标题") or "(无标题)"
    blocks = await source.get_block_tree(page_id)
    md = blocks_to_markdown(blocks)
    return title, _tidy(md)


def _tidy(md: str) -> str:
    """去掉多余空行"""
    import re

    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"
