"""
微信公众号发布服务 - FastAPI 入口
"""

import os
import httpx
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv

from . import article
from .wechat import WechatAPI
from .html_convert import markdown_to_wechat_html

load_dotenv()

# 初始化
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

wechat_api: Optional[WechatAPI] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    global wechat_api

    # 启动时初始化
    article.init_db()

    appid = os.getenv("WX_APPID")
    appsecret = os.getenv("WX_APPSECRET")
    if appid and appsecret:
        wechat_api = WechatAPI(appid, appsecret)

    yield

    # 关闭时清理（如需要）


app = FastAPI(
    title="微信公众号发布服务",
    lifespan=lifespan
)


# ========== 请求/响应模型 ==========

class CreateArticleRequest(BaseModel):
    title: str
    content: str  # Markdown


class UpdateArticleRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None  # Markdown


class ArticleResponse(BaseModel):
    id: str
    title: str
    content: str
    preview_url: str
    created_at: str
    updated_at: str
    published_at: Optional[str] = None


class PublishResponse(BaseModel):
    success: bool
    draft_media_id: Optional[str] = None
    message: str


# ========== API 路由 ==========

@app.get("/")
async def root():
    return {"service": "微信公众号发布服务", "status": "running"}


@app.post("/api/articles", response_model=ArticleResponse)
async def create_article_api(req: CreateArticleRequest, request: Request):
    """创建文章"""
    html_content = markdown_to_wechat_html(req.content)
    art = article.create_article(req.title, req.content, html_content)

    base_url = str(request.base_url).rstrip("/")
    return ArticleResponse(
        id=art.id,
        title=art.title,
        content=art.content,
        preview_url=f"{base_url}/preview/{art.id}",
        created_at=art.created_at,
        updated_at=art.updated_at,
        published_at=art.published_at
    )


@app.get("/api/articles/{article_id}", response_model=ArticleResponse)
async def get_article_api(article_id: str, request: Request):
    """获取文章"""
    art = article.get_article(article_id)
    if not art:
        raise HTTPException(status_code=404, detail="文章不存在")

    base_url = str(request.base_url).rstrip("/")
    return ArticleResponse(
        id=art.id,
        title=art.title,
        content=art.content,
        preview_url=f"{base_url}/preview/{art.id}",
        created_at=art.created_at,
        updated_at=art.updated_at,
        published_at=art.published_at
    )


@app.put("/api/articles/{article_id}", response_model=ArticleResponse)
async def update_article_api(article_id: str, req: UpdateArticleRequest, request: Request):
    """更新文章"""
    html_content = None
    if req.content is not None:
        html_content = markdown_to_wechat_html(req.content)

    art = article.update_article(article_id, req.title, req.content, html_content)
    if not art:
        raise HTTPException(status_code=404, detail="文章不存在")

    base_url = str(request.base_url).rstrip("/")
    return ArticleResponse(
        id=art.id,
        title=art.title,
        content=art.content,
        preview_url=f"{base_url}/preview/{art.id}",
        created_at=art.created_at,
        updated_at=art.updated_at,
        published_at=art.published_at
    )


@app.get("/api/articles")
async def list_articles_api(request: Request):
    """列出所有文章"""
    articles = article.list_articles()
    base_url = str(request.base_url).rstrip("/")

    return [
        ArticleResponse(
            id=art.id,
            title=art.title,
            content=art.content,
            preview_url=f"{base_url}/preview/{art.id}",
            created_at=art.created_at,
            updated_at=art.updated_at,
            published_at=art.published_at
        )
        for art in articles
    ]


@app.post("/api/articles/{article_id}/publish", response_model=PublishResponse)
async def publish_article_api(article_id: str):
    """发布文章到微信公众号草稿箱"""
    if not wechat_api:
        raise HTTPException(status_code=500, detail="微信 API 未配置")

    art = article.get_article(article_id)
    if not art:
        raise HTTPException(status_code=404, detail="文章不存在")

    try:
        # 下载一张默认封面图（暂时用随机图片）
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://picsum.photos/900/500", follow_redirects=True)
            thumb_data = resp.content

        # 上传封面图
        thumb_media_id = await wechat_api.upload_thumb(thumb_data)

        # 创建草稿
        draft_media_id = await wechat_api.create_draft(
            title=art.title,
            content=art.html_content,
            thumb_media_id=thumb_media_id
        )

        # 更新文章状态
        article.mark_published(article_id, draft_media_id)

        return PublishResponse(
            success=True,
            draft_media_id=draft_media_id,
            message="已发布到草稿箱"
        )

    except Exception as e:
        return PublishResponse(
            success=False,
            message=f"发布失败: {str(e)}"
        )


# ========== 预览页面 ==========

@app.get("/preview/{article_id}", response_class=HTMLResponse)
async def preview_article(article_id: str, request: Request):
    """文章预览页面"""
    art = article.get_article(article_id)
    if not art:
        raise HTTPException(status_code=404, detail="文章不存在")

    return templates.TemplateResponse(
        "preview.html",
        {"request": request, "article": art}
    )
