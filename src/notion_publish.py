"""
Notion 页面 → 微信公众号草稿箱（支持多租户 + 站内本地预览镜像）

流程：读页面 → 转 Markdown → AI 配图 → 图片同时存本地预览副本与微信图床 → 排版转 HTML → 建草稿 → 回写状态
"""

import hashlib
import ipaddress
import logging
import os
import re
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from . import article as article_store
from . import imagegen, tenant
from .html_convert import markdown_to_wechat_html
from .notion_source import NotionError, NotionSource, _norm_text, _rich, page_to_markdown
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
LOCAL_IMG_RE = re.compile(r"/images/(?:([^/]+)/)?([^/?#\s]+)$")

MAX_TITLE_LEN = 64


def is_safe_public_url(url: str) -> bool:
    """基础 SSRF 防护：仅允许 http/https 且拒绝明显内网/回环/链路本地地址"""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host or host in ("localhost", "ip6-localhost") or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            addr = info[4][0]
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return False
    except OSError:
        pass
    return True


def _public_base() -> str:
    return os.getenv("BASE_URL", "https://publisher.flyooo.uk").rstrip("/")


def _try_read_local_upload(url: str, upload_dir: Path) -> Optional[bytes]:
    """若 URL 指向本站 /images/... 文件且存在于磁盘，直接读取字节"""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    m = LOCAL_IMG_RE.search(parsed.path or "")
    if not m:
        return None
    sub, name = m.group(1), m.group(2)
    root = Path(upload_dir).resolve()
    candidates = [root / sub / name] if sub else [root / name]
    for p in candidates:
        rp = p.resolve()
        if rp.is_relative_to(root) and rp.is_file():
            return rp.read_bytes()
    return None


async def _download_image(url: str) -> Optional[bytes]:
    """下载图片；带 SSRF 校验"""
    if not is_safe_public_url(url):
        logger.warning("拒绝访问不安全图片地址: %s", url[:80])
        return None
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


def _save_preview_mirror(data: bytes, url: str, user_id: str, upload_dir: Path, base_url: str) -> str:
    """将外链图片缓存到本地供站内预览页稳定展示，返回本站图片 URL"""
    ext = ".png" if _guess_name(url, data).endswith(".png") else ".jpg"
    digest = hashlib.sha1(data).hexdigest()[:12]
    fname = f"pub-{digest}{ext}"
    target_dir = Path(upload_dir) / (user_id or "owner")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / fname
    if not target.exists():
        target.write_bytes(data)
    return f"{base_url}/images/{user_id or 'owner'}/{fname}"


async def _transfer_images(
    md: str,
    wechat: WechatAPI,
    *,
    user_id: str = "owner",
    base_url: Optional[str] = None,
    upload_dir: Optional[Path] = None,
) -> tuple[str, str, Optional[bytes]]:
    """处理正文图片：返回 (wx_md 微信图床版, preview_md 站内本地镜像版, 首图字节)"""
    base = (base_url or _public_base()).rstrip("/")
    up_dir = Path(upload_dir or UPLOAD_DIR)

    urls: list[str] = []
    for m in IMG_PATTERN.finditer(md):
        u = m.group(2)
        if u not in urls:
            urls.append(u)

    wx_mapping: dict[str, str] = {}
    preview_mapping: dict[str, str] = {}
    first_bytes: Optional[bytes] = None

    for u in urls:
        local_data = _try_read_local_upload(u, up_dir)
        is_local = local_data is not None
        data = local_data if is_local else await _download_image(u)
        if not data:
            continue
        if first_bytes is None:
            first_bytes = data

        # 站内预览 URL：本机文件保持原 URL，外链写入本地稳定副本
        if is_local:
            preview_mapping[u] = u
        else:
            try:
                preview_mapping[u] = _save_preview_mirror(data, u, user_id, up_dir, base)
            except Exception as e:
                logger.warning("保存本地预览镜像失败 %s: %s", u[:60], e)

        # 微信图床 URL（供发布草稿箱）
        try:
            wx_url = await wechat.upload_image(data, _guess_name(u, data))
            wx_mapping[u] = wx_url
            logger.info("图片已转存: %s -> %s", u[:60], wx_url[:60])
        except Exception as e:
            logger.warning("图片上传微信失败 %s: %s", u[:60], e)

    wx_md = IMG_PATTERN.sub(lambda m: f"![{m.group(1)}]({wx_mapping.get(m.group(2), m.group(2))})", md) if wx_mapping else md
    preview_md = IMG_PATTERN.sub(lambda m: f"![{m.group(1)}]({preview_mapping.get(m.group(2), m.group(2))})", md) if preview_mapping else md
    return wx_md, preview_md, first_bytes


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


async def write_back_images(
    source: NotionSource,
    page_id: str,
    ai,
    *,
    user_id: str = "owner",
) -> None:
    """把 AI 生成的图写回 Notion：页面封面 + 标题后面的插图块（幂等）"""
    cfg = tenant.get_effective_ai_config(user_id)
    if not cfg.get("write_to_notion", True) or ai is None:
        return

    if ai.cover_url:
        try:
            await source.set_page_cover(page_id, ai.cover_url)
            logger.info("已回写 Notion 封面: %s", ai.cover_url.rsplit("/", 1)[-1])
        except Exception as e:
            logger.warning("回写 Notion 封面失败: %s", e)

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
    user_id: str = "owner",
) -> dict:
    """把一个 Notion 页面发布到对应租户的公众号草稿箱

    返回 {"success": bool, "message": str, "draft_media_id": str|None}
    """
    uid = user_id or "owner"
    try:
        page = await source.get_page(page_id)
    except NotionError as e:
        return {"success": False, "message": f"读取页面失败: {e}", "draft_media_id": None}

    status = NotionSource.prop_text(page, "状态")
    if status != trigger:
        return {"success": False, "message": f"状态为「{status}」，不是「{trigger}」，跳过", "draft_media_id": None}

    title_hint = NotionSource.prop_text(page, "标题") or "(无标题)"

    try:
        await source.set_select(page_id, "状态", STATUS_RUNNING)
    except NotionError as e:
        logger.warning("设置发布中失败: %s", e)

    await notify_event("start", "开始发布", f"「{title_hint}」正在处理…", user_id=uid)

    try:
        title, md = await page_to_markdown(source, page_id)
        if not title or not md.strip():
            raise ValueError("页面没有正文内容")

        ai = None
        try:
            ai = await imagegen.build(title, md, ref=page_id, upload_dir=UPLOAD_DIR, user_id=uid)
            md = ai.markdown
        except Exception as e:
            logger.warning("AI 配图失败，按无图流程继续：%s", e)

        wx_md, preview_md, first_image = await _transfer_images(
            md, wechat, user_id=uid, base_url=_public_base(), upload_dir=UPLOAD_DIR
        )

        tcfg = tenant.get_tenant_config(uid)
        theme_id = tcfg.get("theme") or "default"
        author = ((tcfg.get("wechat") or {}).get("author") or "").strip() or (
            "程序杂念" if uid == "owner" else "公众号发布台"
        )
        wx_html = markdown_to_wechat_html(wx_md, theme_id)

        cover = (ai.cover_bytes if ai and ai.cover_bytes else None) or \
            await _pick_cover(first_image, wechat, os.getenv("UNSPLASH_ACCESS_KEY", ""))
        thumb_media_id = await wechat.upload_thumb(cover, "cover.jpg")

        media_id = await wechat.create_draft(
            title=title[:MAX_TITLE_LEN],
            content=wx_html,
            thumb_media_id=thumb_media_id,
            author=author,
            digest=_digest(wx_md),
        )

        # 写入本地文章库时保存本站本地镜像版 preview_md，保证站内预览正常出图
        try:
            art_id = article_store.upsert_from_source(
                title=title, content=preview_md, html_content=wx_html,
                theme_id=theme_id, source="notion", source_id=page_id,
                user_id=uid,
            )
            article_store.mark_published(art_id, media_id, user_id=uid)
        except Exception as store_err:
            logger.warning("写入文章库失败: %s", store_err)

        try:
            await write_back_images(source, page_id, ai, user_id=uid)
        except Exception as wb_err:
            logger.warning("回写 Notion 配图失败: %s", wb_err)

        await source.set_select(page_id, "状态", STATUS_PUBLISHED)
        await notify_event("success", "发布成功", f"「{title}」已进草稿箱，去后台点发表。", user_id=uid)
        if ai and getattr(ai, "skipped_existing_images", False):
            style_note = "，已有配图已跳过 AI 生图"
        elif ai and getattr(ai, "style_name", ""):
            style_note = f"，配图风格：{ai.style_name}"
        else:
            style_note = ""
        return {"success": True,
                "message": f"已发布到草稿箱（{len(wx_md)} 字{style_note}）",
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
        await notify_event("error", "发布失败", f"「{title_hint}」没发出去：{msg[:120]}", level="timeSensitive", user_id=uid)
        return {"success": False, "message": msg, "draft_media_id": None}
