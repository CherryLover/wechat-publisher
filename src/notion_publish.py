"""
Notion 页面 → 微信公众号草稿箱

流程：读页面 → 转 Markdown → 图片转存微信 → 排版转 HTML → 建草稿 → 回写状态
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

import httpx

from . import article as article_store
from . import imagegen
from .config import get_theme, load_config
from .html_convert import markdown_to_wechat_html
from .notion_source import NotionSource, NotionError, page_to_markdown, _norm_text, _rich
from .notify import notify_event
from .wechat import WechatAPI

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/app/uploads"))

# 触发发布的动作状态
TRIGGER_STATUS = "发布"
STATUS_PUBLISHED = "已发布"
STATUS_FAILED = "发布失败"
STATUS_RUNNING = "发布中"

IMG_PATTERN = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")

MAX_TITLE_LEN = 64


async def _download_image(url: str) -> Optional[bytes]:
    """下载图片；微信图床和 Notion 的 S3 都能直接下"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://mp.weixin.qq.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200 or not resp.content:
            logger.warning("图片下载失败 %s -> %s", url[:80], resp.status_code)
            return None
        return resp.content
    except Exception as e:
        logger.warning("图片下载异常 %s: %s", url[:80], e)
        return None


def _guess_name(url: str, data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image.png"
    if url.lower().split("?")[0].endswith(".png"):
        return "image.png"
    return "image.jpg"


async def _transfer_images(md: str, wechat: WechatAPI) -> tuple[str, Optional[bytes]]:
    """把正文里的外链图片全部转存到微信，返回 (新 Markdown, 首图字节)"""
    urls = []
    for m in IMG_PATTERN.finditer(md):
        u = m.group(2)
        if u not in urls:
            urls.append(u)

    mapping: dict[str, str] = {}
    first_bytes: Optional[bytes] = None

    for u in urls:
        data = await _download_image(u)
        if not data:
            continue
        if first_bytes is None:
            first_bytes = data
        try:
            wx_url = await wechat.upload_image(data, _guess_name(u, data))
            mapping[u] = wx_url
            logger.info("图片已转存: %s -> %s", u[:60], wx_url[:60])
        except Exception as e:
            logger.warning("图片上传微信失败 %s: %s", u[:60], e)

    if not mapping:
        return md, first_bytes

    def repl(m):
        alt, u = m.group(1), m.group(2)
        return f"![{alt}]({mapping.get(u, u)})"

    return IMG_PATTERN.sub(repl, md), first_bytes


async def _pick_cover(first_image: Optional[bytes], wechat: WechatAPI, unsplash_key: str) -> bytes:
    """封面：优先正文首图，其次默认图"""
    if first_image and len(first_image) > 1024:
        return first_image
    if unsplash_key:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    "https://api.unsplash.com/photos/random",
                    headers={"Authorization": f"Client-ID {unsplash_key}"},
                    params={"orientation": "landscape", "query": "nature"},
                )
                raw = r.json()["urls"]["raw"]
                img = await client.get(f"{raw}&w=900&h=383&fit=crop&q=80", follow_redirects=True)
                if img.content:
                    return img.content
        except Exception as e:
            logger.warning("Unsplash 封面失败: %s", e)
    default = Path(__file__).parent.parent / "static" / "default_cover.png"
    return default.read_bytes()


def _digest(md: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    text = re.sub(r"[#>*`\-\[\]()]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:50]


async def write_back_images(source: NotionSource, page_id: str, ai) -> None:
    """把 AI 生成的图写回 Notion：页面封面 + 标题后面的插图块（幂等）"""
    cfg = load_config().get("ai_images") or {}
    if not cfg.get("write_to_notion", True) or ai is None:
        return

    if ai.cover_url:
        try:
            await source.set_page_cover(page_id, ai.cover_url)
            logger.info("已回写 Notion 封面: %s", ai.cover_url.rsplit("/", 1)[-1])
        except Exception as e:
            logger.warning("回写 Notion 封面失败: %s", e)

    # 一次取全子块：既拿标题锚点，也用来判重（别再一张一张查）
    try:
        blocks = await source.get_children(page_id)
    except Exception as e:
        logger.warning("读取 Notion 子块失败，跳过插图回写: %s", e)
        return

    heads: dict[str, str] = {}
    existing: set[str] = set()
    for b in blocks:
        kind = b.get("type")
        if kind in ("heading_1", "heading_2", "heading_3"):
            text = _norm_text(_rich((b.get(kind) or {}).get("rich_text")))
            if text:
                heads[text] = b.get("id")
        elif kind == "image":
            img = b.get("image") or {}
            url = ((img.get("external") or {}).get("url")
                   or (img.get("file") or {}).get("url") or "")
            if url:
                existing.add(url.rsplit("/", 1)[-1])

    for it in ai.items:
        url = it.get("url")
        if not url:
            continue
        name = url.rsplit("/", 1)[-1]
        if name in existing:
            logger.info("Notion 里已有 %s，跳过", name)
            continue
        try:
            target = _norm_text(it.get("heading") or "")
            anchor = heads.get(target)
            if anchor is None and target:
                for text, bid in heads.items():
                    if target in text or text in target:
                        anchor = bid
                        break
            await source.append_image_after(page_id, anchor, url, it.get("alt") or "")
            logger.info("已回写 Notion 插图 %s（锚点：%s）",
                        name, it.get("heading") or "页面末尾")
        except Exception as e:
            logger.warning("回写 Notion 插图失败 %s: %s", name, e)


async def publish_page(
    page_id: str,
    source: NotionSource,
    wechat: WechatAPI,
    trigger: str = TRIGGER_STATUS,
) -> dict:
    """把一个 Notion 页面发布到公众号草稿箱

    返回 {"success": bool, "message": str, "draft_media_id": str|None}
    """
    try:
        page = await source.get_page(page_id)
    except NotionError as e:
        return {"success": False, "message": f"读取页面失败: {e}", "draft_media_id": None}

    status = NotionSource.prop_text(page, "状态")
    if status != trigger:
        return {"success": False, "message": f"状态为「{status}」，不是「{trigger}」，跳过", "draft_media_id": None}

    title_hint = NotionSource.prop_text(page, "标题") or "(无标题)"

    # 标记发布中，避免重复触发
    try:
        await source.set_select(page_id, "状态", STATUS_RUNNING)
    except NotionError as e:
        logger.warning("设置发布中失败: %s", e)

    await notify_event("start", "开始发布", f"「{title_hint}」正在处理…")

    try:
        title, md = await page_to_markdown(source, page_id)
        if not title or not md.strip():
            raise ValueError("页面没有正文内容")

        # AI 配图（可选，受设置页全局开关控制）：1 张封面 + 3 张插图
        # 插图由模型自己读文章决定放在哪个小节后面，插完再把 Markdown 交给排版
        ai = None
        try:
            ai = await imagegen.build(title, md, ref=page_id, upload_dir=UPLOAD_DIR)
            md = ai.markdown
        except Exception as e:
            logger.warning("AI 配图失败，按无图流程继续：%s", e)

        md, first_image = await _transfer_images(md, wechat)

        theme_id = get_theme()
        html = markdown_to_wechat_html(md, theme_id)

        cover = (ai.cover_bytes if ai and ai.cover_bytes else None) or \
            await _pick_cover(first_image, wechat, os.getenv("UNSPLASH_ACCESS_KEY", ""))
        thumb_media_id = await wechat.upload_thumb(cover, "cover.jpg")

        media_id = await wechat.create_draft(
            title=title[:MAX_TITLE_LEN],
            content=html,
            thumb_media_id=thumb_media_id,
            author="程序杂念",
            digest=_digest(md),
        )

        # 写入本地文章库，让主页能看到、预览能用
        try:
            article_store.upsert_from_source(
                title=title, content=md, html_content=html,
                theme_id=theme_id, source="notion", source_id=page_id,
            )
        except Exception as store_err:
            logger.warning("写入文章库失败: %s", store_err)

        # 把 AI 图回写 Notion（封面 + 插图块），失败不影响出版
        try:
            await write_back_images(source, page_id, ai)
        except Exception as wb_err:
            logger.warning("回写 Notion 配图失败: %s", wb_err)

        await source.set_select(page_id, "状态", STATUS_PUBLISHED)
        await notify_event("success", "发布成功", f"「{title}」已进草稿箱，去后台点发表。")
        if ai and getattr(ai, "skipped_existing_images", False):
            style_note = "，已有配图已跳过 AI 生图"
        elif ai and getattr(ai, "style_name", ""):
            style_note = f"，配图风格：{ai.style_name}"
        else:
            style_note = ""
        return {"success": True,
                "message": f"已发布到草稿箱（{len(md)} 字{style_note}）",
                "draft_media_id": media_id}

    except Exception as e:
        import traceback

        traceback.print_exc()
        msg = f"{type(e).__name__}: {e}"
        try:
            await source.set_select(page_id, "状态", STATUS_FAILED)
            await source.set_rich_text(page_id, "迁移备注", f"发布失败：{msg}"[:1900])
        except Exception:
            logger.error("回写失败状态也失败了")
        await notify_event("error", "发布失败", f"「{title_hint}」没发出去：{msg[:120]}", level="timeSensitive")
        return {"success": False, "message": msg, "draft_media_id": None}
