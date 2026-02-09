"""
微信公众号发布服务 - FastAPI 入口
"""

import os
import re
import uuid
import httpx
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

from . import article
from .wechat import WechatAPI
from .html_convert import (
    markdown_to_html,
    markdown_to_wechat_html,
    apply_theme_for_preview,
    list_themes as get_available_themes,
    load_themes,
)
from .mcp_server import mcp

# 图片存储目录
UPLOAD_DIR = Path("/app/uploads")

load_dotenv()

# 初始化
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

wechat_api: Optional[WechatAPI] = None


def get_base_url() -> str:
    """获取服务 base URL（MCP 工具使用，无法从 HTTP 请求获取）"""
    return os.getenv("BASE_URL", "https://publisher.flyooo.uk").rstrip("/")


def get_wechat_api() -> Optional[WechatAPI]:
    """获取微信 API 实例"""
    return wechat_api


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    global wechat_api

    # 启动时初始化
    article.init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    load_themes()

    appid = os.getenv("WX_APPID")
    appsecret = os.getenv("WX_APPSECRET")
    if appid and appsecret:
        wechat_api = WechatAPI(appid, appsecret)

    async with mcp.session_manager.run():
        yield

    # 关闭时清理（如需要）


app = FastAPI(
    title="微信公众号发布服务",
    lifespan=lifespan
)

# 挂载 MCP Server 到 /mcp 路径
app.mount("/mcp", mcp.streamable_http_app())


# ========== 请求/响应模型 ==========

class CreateArticleRequest(BaseModel):
    title: str
    content: str  # Markdown
    theme_id: str = "default"


class UpdateArticleRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None  # Markdown
    theme_id: Optional[str] = None


class ArticleResponse(BaseModel):
    id: str
    title: str
    content: str
    theme_id: str
    preview_url: str
    created_at: str
    updated_at: str
    published_at: Optional[str] = None


class PublishResponse(BaseModel):
    success: bool
    draft_media_id: Optional[str] = None
    message: str


class UploadImageResponse(BaseModel):
    url: str
    filename: str


# ========== API 路由 ==========

def _article_response(art, base_url: str) -> ArticleResponse:
    """构建 ArticleResponse"""
    return ArticleResponse(
        id=art.id,
        title=art.title,
        content=art.content,
        theme_id=art.theme_id,
        preview_url=f"{base_url}/preview/{art.id}",
        created_at=art.created_at,
        updated_at=art.updated_at,
        published_at=art.published_at
    )


@app.get("/")
async def root():
    return {"service": "微信公众号发布服务", "status": "running"}


@app.get("/api/themes")
async def list_themes_api():
    """获取可用主题列表"""
    return get_available_themes()


# ========== 图片上传 ==========

@app.post("/api/images", response_model=UploadImageResponse)
async def save_image(request: Request, file: UploadFile = File(...)):
    """保存图片到本地存储"""
    # 生成唯一文件名
    ext = Path(file.filename).suffix or ".png"
    filename = f"{uuid.uuid4().hex}{ext}"
    file_path = UPLOAD_DIR / filename

    # 保存文件
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    base_url = str(request.base_url).rstrip("/")
    return UploadImageResponse(
        url=f"{base_url}/images/{filename}",
        filename=filename
    )


@app.get("/images/{filename}")
async def get_image(filename: str):
    """获取本地图片"""
    file_path = UPLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(file_path)


@app.post("/api/articles", response_model=ArticleResponse)
async def create_article_api(req: CreateArticleRequest, request: Request):
    """创建文章"""
    html_content = markdown_to_wechat_html(req.content, req.theme_id)
    art = article.create_article(req.title, req.content, html_content, req.theme_id)

    base_url = str(request.base_url).rstrip("/")
    return _article_response(art, base_url)


@app.get("/api/articles/{article_id}", response_model=ArticleResponse)
async def get_article_api(article_id: str, request: Request):
    """获取文章"""
    art = article.get_article(article_id)
    if not art:
        raise HTTPException(status_code=404, detail="文章不存在")

    base_url = str(request.base_url).rstrip("/")
    return _article_response(art, base_url)


@app.put("/api/articles/{article_id}", response_model=ArticleResponse)
async def update_article_api(article_id: str, req: UpdateArticleRequest, request: Request):
    """更新文章"""
    # 需要重新渲染 HTML 的情况：内容变了，或主题变了
    existing = article.get_article(article_id)
    if not existing:
        raise HTTPException(status_code=404, detail="文章不存在")

    new_content = req.content if req.content is not None else existing.content
    new_theme = req.theme_id if req.theme_id is not None else existing.theme_id

    html_content = None
    if req.content is not None or req.theme_id is not None:
        html_content = markdown_to_wechat_html(new_content, new_theme)

    art = article.update_article(
        article_id, req.title, req.content, html_content, req.theme_id
    )

    base_url = str(request.base_url).rstrip("/")
    return _article_response(art, base_url)


@app.get("/api/articles")
async def list_articles_api(request: Request):
    """列出所有文章"""
    articles = article.list_articles()
    base_url = str(request.base_url).rstrip("/")

    return [_article_response(art, base_url) for art in articles]


async def convert_local_images_to_wechat(html_content: str, base_url: str) -> str:
    """将本地图片 URL 转换为微信图片 URL"""
    # 匹配本地图片 URL: /images/xxx 或 http://xxx/images/xxx
    pattern = r'<img[^>]+src=["\']([^"\']*?/images/([^"\']+))["\']'

    async def replace_image(match):
        local_url = match.group(1)
        filename = match.group(2)
        file_path = UPLOAD_DIR / filename

        if not file_path.exists():
            return match.group(0)  # 文件不存在，保持原样

        # 读取本地图片
        with open(file_path, "rb") as f:
            image_data = f.read()

        # 上传到微信
        try:
            wx_url = await wechat_api.upload_image(image_data, filename)
            # 替换 src
            original = match.group(0)
            return original.replace(local_url, wx_url)
        except Exception as e:
            print(f"上传图片失败 {filename}: {e}")
            return match.group(0)

    # 找到所有匹配
    matches = list(re.finditer(pattern, html_content))
    if not matches:
        return html_content

    # 逐个替换（从后往前，避免位置偏移）
    result = html_content
    for match in reversed(matches):
        local_url = match.group(1)
        filename = match.group(2)
        file_path = UPLOAD_DIR / filename

        if not file_path.exists():
            continue

        with open(file_path, "rb") as f:
            image_data = f.read()

        try:
            wx_url = await wechat_api.upload_image(image_data, filename)
            original = match.group(0)
            new_img = original.replace(local_url, wx_url)
            result = result[:match.start()] + new_img + result[match.end():]
        except Exception as e:
            print(f"上传图片失败 {filename}: {e}")

    return result


async def publish_article(article_id: str) -> dict:
    """发布文章到微信公众号草稿箱（HTTP API 和 MCP 工具共用）

    Returns:
        dict: {"success": bool, "draft_media_id": str|None, "message": str}
    """
    if not wechat_api:
        return {"success": False, "draft_media_id": None, "message": "微信 API 未配置"}

    art = article.get_article(article_id)
    if not art:
        return {"success": False, "draft_media_id": None, "message": "文章不存在"}

    try:
        base_url = get_base_url()

        # 转换本地图片为微信图片
        wx_html_content = await convert_local_images_to_wechat(art.html_content, base_url)

        # 下载一张默认封面图（暂时用随机图片）
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://picsum.photos/900/500", follow_redirects=True)
            thumb_data = resp.content

        # 上传封面图
        thumb_media_id = await wechat_api.upload_thumb(thumb_data)

        # 创建草稿
        draft_media_id = await wechat_api.create_draft(
            title=art.title,
            content=wx_html_content,
            thumb_media_id=thumb_media_id
        )

        # 更新文章状态
        article.mark_published(article_id, draft_media_id)

        return {"success": True, "draft_media_id": draft_media_id, "message": "已发布到草稿箱"}

    except Exception as e:
        return {"success": False, "draft_media_id": None, "message": f"发布失败: {str(e)}"}


@app.post("/api/articles/{article_id}/publish", response_model=PublishResponse)
async def publish_article_api(article_id: str, request: Request):
    """发布文章到微信公众号草稿箱"""
    result = await publish_article(article_id)

    if not result["success"] and result["message"] == "微信 API 未配置":
        raise HTTPException(status_code=500, detail="微信 API 未配置")
    if not result["success"] and result["message"] == "文章不存在":
        raise HTTPException(status_code=404, detail="文章不存在")

    return PublishResponse(**result)


# ========== 预览页面 ==========

@app.get("/preview/{article_id}", response_class=HTMLResponse)
async def preview_article(article_id: str, request: Request):
    """文章预览页面"""
    art = article.get_article(article_id)
    if not art:
        raise HTTPException(status_code=404, detail="文章不存在")

    # 从 Markdown 实时渲染 + CSS（预览用完整 CSS，非内联）
    raw_html = markdown_to_html(art.content)
    preview_html = apply_theme_for_preview(raw_html, art.theme_id)

    # 获取主题名称
    theme_name = next(
        (t["name"] for t in get_available_themes() if t["id"] == art.theme_id),
        art.theme_id
    )

    return templates.TemplateResponse(
        "preview.html",
        {
            "request": request,
            "article": art,
            "preview_html": preview_html,
            "theme_name": theme_name,
            "themes": get_available_themes(),
        }
    )
