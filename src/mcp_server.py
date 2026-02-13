"""
MCP Server - 微信公众号发布工具

提供 6 个 MCP 工具供 AI 客户端调用：
- create_article: 创建文章
- update_article: 更新文章
- get_article: 获取文章详情
- list_articles: 列出所有文章
- save_image: 保存图片到服务端
- publish_to_draft: 发布到微信草稿箱
"""

import json
import os
import uuid
import base64
import httpx
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import article
from .html_convert import markdown_to_wechat_html
from .auth import create_preview_token, AUTH_TOKEN

# 图片存储目录（与 main.py 共用）
UPLOAD_DIR = Path("/app/uploads")

mcp = FastMCP(
    name="WeChat Publisher",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


def _get_base_url() -> str:
    """获取服务 base URL"""
    return os.getenv("BASE_URL", "https://publisher.flyooo.uk").rstrip("/")


def _make_preview_url(article_id: str, preview_token: Optional[str] = None) -> str:
    """生成带临时 token 的预览链接。

    Args:
        article_id: 文章 ID
        preview_token: 复用的临时 token。未传入时自动生成新 token。
    """
    base_url = _get_base_url()
    url = f"{base_url}/preview/{article_id}"
    if AUTH_TOKEN:
        token = preview_token or create_preview_token()["token"]
        url += f"?token={token}"
    return url


def _get_or_create_token() -> Optional[str]:
    """获取一个临时 token（供批量操作复用）。AUTH_TOKEN 未配置时返回 None。"""
    if not AUTH_TOKEN:
        return None
    return create_preview_token()["token"]


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
    html_content = markdown_to_wechat_html(content, theme_id)
    art = article.create_article(title, content, html_content, theme_id)

    return json.dumps({
        "article_id": art.id,
        "title": art.title,
        "theme_id": art.theme_id,
        "preview_url": _make_preview_url(art.id),
        "created_at": art.created_at,
    }, ensure_ascii=False)


@mcp.tool()
async def update_article(
    article_id: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
    theme_id: Optional[str] = None,
) -> str:
    """更新已有文章的标题、内容或主题。

    Args:
        article_id: 文章 ID
        title: 新标题（可选）
        content: 新内容，Markdown 格式（可选）
        theme_id: 新主题 ID（可选）

    Returns:
        JSON 字符串，包含更新后的文章信息
    """
    existing = article.get_article(article_id)
    if not existing:
        return json.dumps({"error": "文章不存在"}, ensure_ascii=False)

    new_content = content if content is not None else existing.content
    new_theme = theme_id if theme_id is not None else existing.theme_id

    html_content = None
    if content is not None or theme_id is not None:
        html_content = markdown_to_wechat_html(new_content, new_theme)

    art = article.update_article(article_id, title, content, html_content, theme_id)
    if not art:
        return json.dumps({"error": "更新失败"}, ensure_ascii=False)

    return json.dumps({
        "article_id": art.id,
        "title": art.title,
        "theme_id": art.theme_id,
        "preview_url": _make_preview_url(art.id),
        "updated_at": art.updated_at,
    }, ensure_ascii=False)


@mcp.tool()
async def get_article(article_id: str) -> str:
    """获取文章详情。

    Args:
        article_id: 文章 ID

    Returns:
        JSON 字符串，包含文章标题、内容（Markdown）、主题、预览链接等
    """
    art = article.get_article(article_id)
    if not art:
        return json.dumps({"error": "文章不存在"}, ensure_ascii=False)

    return json.dumps({
        "article_id": art.id,
        "title": art.title,
        "content": art.content,
        "theme_id": art.theme_id,
        "preview_url": _make_preview_url(art.id),
        "created_at": art.created_at,
        "updated_at": art.updated_at,
        "published_at": art.published_at,
    }, ensure_ascii=False)


@mcp.tool()
async def list_articles() -> str:
    """列出所有文章。

    Returns:
        JSON 字符串，包含文章列表（id、标题、主题、创建时间、发布状态）
    """
    articles = article.list_articles()
    token = _get_or_create_token()

    result = []
    for art in articles:
        result.append({
            "article_id": art.id,
            "title": art.title,
            "theme_id": art.theme_id,
            "preview_url": _make_preview_url(art.id, preview_token=token),
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
    """保存图片到服务端，返回可在 Markdown 中引用的图片 URL。

    提供 image_url 或 image_base64 其中之一即可。

    Args:
        image_url: 图片的网络 URL（二选一）
        image_base64: 图片的 Base64 编码字符串（二选一）

    Returns:
        JSON 字符串，包含图片 URL 和文件名
    """
    if not image_url and not image_base64:
        return json.dumps({"error": "请提供 image_url 或 image_base64"}, ensure_ascii=False)

    try:
        if image_url:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(image_url)
                resp.raise_for_status()
                image_data = resp.content

            # 从 URL 推断扩展名
            url_path = image_url.split("?")[0]
            ext = Path(url_path).suffix
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                ext = ".png"
        else:
            assert image_base64 is not None
            image_data = base64.b64decode(image_base64)
            ext = ".png"

        filename = f"{uuid.uuid4().hex}{ext}"
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        file_path = UPLOAD_DIR / filename
        with open(file_path, "wb") as f:
            f.write(image_data)

        base_url = _get_base_url()
        image_local_url = f"{base_url}/images/{filename}"

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
    """使用 AI 生成图片，返回可在 Markdown 中引用的图片 URL。

    可用于生成文章配图或封面图。英文 prompt 效果更好。

    Args:
        prompt: 图片描述，例如 "A cat reading a book, watercolor style"
        aspect_ratio: 宽高比，可选值：1:1, 3:4, 4:3, 9:16, 16:9。默认 1:1

    Returns:
        JSON 字符串，包含图片 URL 和 Markdown 引用
    """
    from .main import get_imagen_api

    api = get_imagen_api()
    if not api:
        return json.dumps({"error": "Imagen API 未配置，请设置 IMAGEN_API_KEY 环境变量"}, ensure_ascii=False)

    try:
        image_data = await api.generate(prompt, aspect_ratio)

        filename = f"{uuid.uuid4().hex}.png"
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        file_path = UPLOAD_DIR / filename
        with open(file_path, "wb") as f:
            f.write(image_data)

        base_url = _get_base_url()
        image_url = f"{base_url}/images/{filename}"

        return json.dumps({
            "url": image_url,
            "filename": filename,
            "markdown": f"![image]({image_url})",
        }, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"error": f"图片生成失败: {str(e)}"}, ensure_ascii=False)


@mcp.tool()
async def publish_to_draft(article_id: str) -> str:
    """将文章发布到微信公众号草稿箱。

    发布前会自动将文章中的本地图片转存到微信服务器。

    Args:
        article_id: 文章 ID

    Returns:
        JSON 字符串，包含发布结果和草稿 media_id
    """
    # 延迟导入避免循环引用
    from .main import publish_article

    result = await publish_article(article_id)
    return json.dumps(result, ensure_ascii=False)
