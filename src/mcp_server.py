"""
MCP Server - 微信公众号发布工具（支持多租户上下文隔离）

提供 6 个 MCP 工具供 AI 客户端调用：
- create_article: 创建文章
- update_article: 更新文章
- get_article: 获取文章详情
- list_articles: 列出所有文章
- save_image: 保存图片到服务端
- publish_to_draft: 发布到微信草稿箱
"""

import base64
import json
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

from . import article, tenant
from .auth import create_preview_token
from .html_convert import markdown_to_wechat_html
from .notion_publish import is_safe_public_url

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/app/uploads"))

mcp = FastMCP(
    name="WeChat Publisher",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


def _get_base_url() -> str:
    """获取服务 base URL"""
    return os.getenv("BASE_URL", "https://publisher.flyooo.uk").rstrip("/")


def _current_uid() -> str:
    return tenant.current_tenant_id.get() or tenant.OWNER_ID


def _make_preview_url(article_id: str, user_id: str, preview_token: Optional[str] = None) -> str:
    """生成带临时 token（绑定租户与文章）的预览链接"""
    base_url = _get_base_url()
    token = preview_token or create_preview_token(user_id=user_id, article_id=article_id)["token"]
    return f"{base_url}/preview/{article_id}?token={token}"


@mcp.tool()
async def create_article(
    title: str,
    content: str,
    theme_id: str = "default",
) -> str:
    """创建微信公众号文章。

    Args:
        title: 文章标题
        content: 文章内容（Markdown 格式）
        theme_id: 排版主题 ID，可选值：default, purple, lapis, rainbow, maize,
                  orangeheart, phycat, pie, juejin_default, medium_default,
                  toutiao_default, zhihu_default。默认 default

    Returns:
        JSON 字符串，包含 article_id 和 preview_url
    """
    uid = _current_uid()
    html_content = markdown_to_wechat_html(content, theme_id)
    art = article.create_article(title, content, html_content, theme_id, user_id=uid)

    return json.dumps({
        "article_id": art.id,
        "title": art.title,
        "theme_id": art.theme_id,
        "preview_url": _make_preview_url(art.id, uid),
        "created_at": art.created_at,
    }, ensure_ascii=False)


@mcp.tool()
async def update_article(
    article_id: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
    theme_id: Optional[str] = None,
) -> str:
    """更新已有文章的标题、内容或主题。"""
    uid = _current_uid()
    existing = article.get_article(article_id, user_id=uid)
    if not existing:
        return json.dumps({"error": "文章不存在"}, ensure_ascii=False)

    new_content = content if content is not None else existing.content
    new_theme = theme_id if theme_id is not None else existing.theme_id

    html_content = None
    if content is not None or theme_id is not None:
        html_content = markdown_to_wechat_html(new_content, new_theme)

    art = article.update_article(article_id, title, content, html_content, theme_id, user_id=uid)
    if not art:
        return json.dumps({"error": "更新失败"}, ensure_ascii=False)

    return json.dumps({
        "article_id": art.id,
        "title": art.title,
        "theme_id": art.theme_id,
        "preview_url": _make_preview_url(art.id, uid),
        "updated_at": art.updated_at,
    }, ensure_ascii=False)


@mcp.tool()
async def get_article(article_id: str) -> str:
    """获取文章详情。"""
    uid = _current_uid()
    art = article.get_article(article_id, user_id=uid)
    if not art:
        return json.dumps({"error": "文章不存在"}, ensure_ascii=False)

    return json.dumps({
        "article_id": art.id,
        "title": art.title,
        "content": art.content,
        "theme_id": art.theme_id,
        "preview_url": _make_preview_url(art.id, uid),
        "created_at": art.created_at,
        "updated_at": art.updated_at,
        "published_at": art.published_at,
    }, ensure_ascii=False)


@mcp.tool()
async def list_articles() -> str:
    """列出当前租户的所有文章。"""
    uid = _current_uid()
    articles = article.list_articles(user_id=uid)

    result = []
    for art in articles:
        result.append({
            "article_id": art.id,
            "title": art.title,
            "theme_id": art.theme_id,
            "preview_url": _make_preview_url(art.id, uid),
            "created_at": art.created_at,
            "updated_at": art.updated_at,
            "published_at": art.published_at,
        })

    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def save_image(
    image_url: Optional[str] = None,
    image_base64: Optional[str] = None,
) -> str:
    """保存图片到服务端，返回可在 Markdown 中引用的图片 URL。"""
    if not image_url and not image_base64:
        return json.dumps({"error": "请提供 image_url 或 image_base64"}, ensure_ascii=False)

    uid = _current_uid()
    try:
        if image_url:
            if not is_safe_public_url(image_url):
                return json.dumps({"error": "不允许下载内网或非法 URL"}, ensure_ascii=False)
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(image_url)
                resp.raise_for_status()
                image_data = resp.content

            url_path = image_url.split("?")[0]
            ext = Path(url_path).suffix
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                ext = ".png"
        else:
            assert image_base64 is not None
            image_data = base64.b64decode(image_base64)
            ext = ".png"

        filename = f"{uuid.uuid4().hex}{ext}"
        target_dir = UPLOAD_DIR / uid
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / filename
        with open(file_path, "wb") as f:
            f.write(image_data)

        base_url = _get_base_url()
        image_local_url = f"{base_url}/images/{uid}/{filename}"

        return json.dumps({
            "url": image_local_url,
            "filename": filename,
            "markdown": f"![image]({image_local_url})",
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"保存图片失败: {str(e)}"}, ensure_ascii=False)


@mcp.tool()
async def generate_image(
    prompt: str,
    aspect_ratio: str = "1:1",
) -> str:
    """使用 AI 生成图片，返回可在 Markdown 中引用的图片 URL。"""
    from .main import get_imagen_api

    api = get_imagen_api()
    if not api:
        return json.dumps({"error": "Imagen API 未配置，请设置 IMAGEN_API_KEY 环境变量"}, ensure_ascii=False)

    uid = _current_uid()
    try:
        image_data = await api.generate(prompt, aspect_ratio)

        filename = f"{uuid.uuid4().hex}.png"
        target_dir = UPLOAD_DIR / uid
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / filename
        with open(file_path, "wb") as f:
            f.write(image_data)

        base_url = _get_base_url()
        image_url = f"{base_url}/images/{uid}/{filename}"

        return json.dumps({
            "url": image_url,
            "filename": filename,
            "markdown": f"![image]({image_url})",
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"图片生成失败: {str(e)}"}, ensure_ascii=False)


@mcp.tool()
async def publish_to_draft(article_id: str) -> str:
    """将文章发布到当前租户的微信公众号草稿箱。"""
    from .main import publish_article

    uid = _current_uid()
    result = await publish_article(article_id, user_id=uid)
    return json.dumps(result, ensure_ascii=False)
