"""
微信公众号发布服务 - FastAPI 入口（支持多租户 MCP、REST、静态图与微信图床预览代理）
"""

import hashlib
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from . import article, imagegen, tenant
from .auth import (
    AUTH_TOKEN,
    create_preview_token,
    get_preview_token_info,
    verify_auth_token,
    verify_bearer_or_preview_token,
    verify_preview_token,
)
from .html_convert import (
    apply_theme_for_preview,
    list_themes as get_available_themes,
    load_themes,
    markdown_to_html,
    markdown_to_wechat_html,
)
from .imagen import ImagenAPI
from .mcp_server import mcp
from .notion_publish import is_safe_public_url
from .notion_webhook import router as notion_router
from .web import current_user, router as web_router
from .wechat import WechatAPI

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/app/uploads"))
_WX_IMG_HOSTS = ("mmbiz.qpic.cn", "mmbiz.qlogo.cn", "wx.qlogo.cn")
_WX_IMG_SRC_RE = re.compile(r'(<img\b[^>]*\bsrc=["\'])(https?://[^"\']+)(["\'][^>]*>)', re.IGNORECASE)

load_dotenv()

templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

imagen_api: Optional[ImagenAPI] = None


def get_base_url() -> str:
    """获取服务 base URL（MCP 工具使用，无法从 HTTP 请求获取）"""
    return os.getenv("BASE_URL", "https://publisher.flyooo.uk").rstrip("/")


def get_wechat_api(user_id: str = tenant.OWNER_ID) -> Optional[WechatAPI]:
    """按租户获取微信 API 实例"""
    return tenant.get_wechat_api(user_id)


def get_imagen_api() -> Optional[ImagenAPI]:
    """获取 Imagen API 实例"""
    return imagen_api


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    global imagen_api

    article.init_db()
    tenant.init_users_table()
    tenant.bootstrap_owner()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    load_themes()

    imagen_api_key = os.getenv("IMAGEN_API_KEY")
    if imagen_api_key:
        imagen_api = ImagenAPI(imagen_api_key)

    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="微信公众号发布服务",
    lifespan=lifespan,
)

app.include_router(notion_router)
app.include_router(web_router)


# ========== MCP 鉴权与多租户隔离中间件 ==========

class MCPAuthMiddleware:
    """ASGI 中间件：支持路径前缀 /mcp/{mcp_token} 与 Bearer Token 两种租户隔离模式"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or "/"
        root = scope.get("root_path", "") or ""
        rel = path[len(root):] if root and path.startswith(root) else path
        segs = [s for s in rel.split("/") if s]
        uid: Optional[str] = None
        new_scope = scope

        # 模式 1：/mcp/{mcp_token}[/...]
        if segs and segs[0].startswith("mcp_"):
            user = tenant.get_user_by_mcp_token(segs[0])
            if not user:
                await JSONResponse(status_code=401, content={"detail": "无效的 MCP 接入链接"})(scope, receive, send)
                return
            if user.get("status") != "active":
                await JSONResponse(status_code=403, content={"detail": "账号已被停用"})(scope, receive, send)
                return
            uid = user["id"]
            rest = "/" + "/".join(segs[1:]) if len(segs) > 1 else "/"
            new_path = (root.rstrip("/") + rest) if root else rest
            new_scope = dict(scope)
            new_scope["path"] = new_path
            new_scope["raw_path"] = new_path.encode("ascii", errors="ignore")
        else:
            # 模式 2：/mcp + Authorization: Bearer <mcp_token or AUTH_TOKEN>
            headers = dict(scope.get("headers", []))
            auth_value = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
            if auth_value.startswith("Bearer "):
                tok = auth_value[7:].strip()
                user = tenant.get_user_by_mcp_token(tok)
                if user:
                    if user.get("status") != "active":
                        await JSONResponse(status_code=403, content={"detail": "账号已被停用"})(scope, receive, send)
                        return
                    uid = user["id"]
                elif AUTH_TOKEN and secrets.compare_digest(tok, AUTH_TOKEN):
                    if not tenant.is_user_active(tenant.OWNER_ID):
                        await JSONResponse(status_code=403, content={"detail": "账号已被停用"})(scope, receive, send)
                        return
                    uid = tenant.OWNER_ID
                else:
                    await JSONResponse(status_code=401, content={"detail": "未授权访问"})(scope, receive, send)
                    return
            elif AUTH_TOKEN:
                await JSONResponse(status_code=401, content={"detail": "未授权访问"})(scope, receive, send)
                return
            else:
                uid = tenant.OWNER_ID

        ctx_tok = tenant.current_tenant_id.set(uid)
        try:
            await self.app(new_scope, receive, send)
        finally:
            tenant.current_tenant_id.reset(ctx_tok)


mcp_app = mcp.streamable_http_app()
app.mount("/mcp", MCPAuthMiddleware(mcp_app))


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
    return ArticleResponse(
        id=art.id,
        title=art.title,
        content=art.content,
        theme_id=art.theme_id,
        preview_url=f"{base_url}/preview/{art.id}",
        created_at=art.created_at,
        updated_at=art.updated_at,
        published_at=art.published_at,
    )


@app.get("/api/themes")
async def list_themes_api(_uid: str = Depends(verify_auth_token)):
    return get_available_themes()


@app.post("/api/preview-token")
async def create_preview_token_api(article_id: Optional[str] = None, uid: str = Depends(verify_auth_token)):
    return create_preview_token(user_id=uid, article_id=article_id)


@app.post("/api/images", response_model=UploadImageResponse)
async def save_image(request: Request, file: UploadFile = File(...), uid: str = Depends(verify_auth_token)):
    ext = Path(file.filename or "").suffix or ".png"
    filename = f"{uuid.uuid4().hex}{ext}"
    target_dir = UPLOAD_DIR / uid
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / filename

    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    base_url = str(request.base_url).rstrip("/")
    return UploadImageResponse(
        url=f"{base_url}/images/{uid}/{filename}",
        filename=filename,
    )


class GenerateImageRequest(BaseModel):
    prompt: str
    aspect_ratio: str = "1:1"


@app.post("/api/generate-image", response_model=UploadImageResponse)
async def generate_image_api(req: GenerateImageRequest, request: Request, uid: str = Depends(verify_auth_token)):
    if not imagen_api:
        raise HTTPException(status_code=500, detail="Imagen API 未配置")

    try:
        image_data = await imagen_api.generate(req.prompt, req.aspect_ratio)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"图片生成失败: {str(e)}")

    filename = f"{uuid.uuid4().hex}.png"
    target_dir = UPLOAD_DIR / uid
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / filename
    with open(file_path, "wb") as f:
        f.write(image_data)

    base_url = str(request.base_url).rstrip("/")
    return UploadImageResponse(
        url=f"{base_url}/images/{uid}/{filename}",
        filename=filename,
    )


@app.get("/images/{user_id}/{filename}")
async def get_tenant_image(user_id: str, filename: str):
    """获取租户本地图片"""
    root = UPLOAD_DIR.resolve()
    file_path = (root / user_id / filename).resolve()
    if not file_path.is_relative_to(root):
        raise HTTPException(status_code=400, detail="非法文件名")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(file_path)


@app.get("/images/{filename}")
async def get_image(filename: str):
    """兼容老版根路径本地图片"""
    root = UPLOAD_DIR.resolve()
    file_path = (root / filename).resolve()
    if not file_path.is_relative_to(root):
        raise HTTPException(status_code=400, detail="非法文件名")
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(file_path)


def _is_wx_img_host(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _WX_IMG_HOSTS)


def _rewrite_wx_images_for_preview(html: str, token: Optional[str]) -> str:
    """将预览 HTML 中指向微信图床防盗链的图片地址转为站内受限代理"""
    def repl(m: re.Match) -> str:
        prefix, url, suffix = m.group(1), m.group(2), m.group(3)
        if not _is_wx_img_host(url):
            return m.group(0)
        proxied = f"/api/preview-img?url={quote(url, safe='')}"
        if token:
            proxied += f"&token={quote(token, safe='')}"
        return f"{prefix}{proxied}{suffix}"

    return _WX_IMG_SRC_RE.sub(repl, html)


@app.get("/api/preview-img")
async def proxy_wechat_preview_image(url: str, request: Request, token: Optional[str] = None):
    """受限微信图床代理：解决老文章微信图片防盗链裂图，仅允许白名单域名且需登录或有效预览 token"""
    user = current_user(request)
    if not user and not verify_preview_token(token):
        raise HTTPException(status_code=401, detail="未授权访问")

    if not _is_wx_img_host(url) or not is_safe_public_url(url):
        raise HTTPException(status_code=400, detail="仅支持代理微信公众号图床图片")

    cache_dir = UPLOAD_DIR / "_wxproxy"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    img_path = cache_dir / f"{key}.img"
    ct_path = cache_dir / f"{key}.ct"

    if img_path.is_file():
        media_type = ct_path.read_text(encoding="utf-8").strip() if ct_path.is_file() else "image/jpeg"
        return Response(content=img_path.read_bytes(), media_type=media_type or "image/jpeg")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://mp.weixin.qq.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200 or not resp.content:
            raise HTTPException(status_code=502, detail="拉取微信图片失败")
        if len(resp.content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="图片体积过大")
        ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if not ct.startswith("image/"):
            ct = "image/jpeg"
        img_path.write_bytes(resp.content)
        ct_path.write_text(ct, encoding="utf-8")
        return Response(content=resp.content, media_type=ct)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"拉取微信图片异常: {e}")


@app.post("/api/articles", response_model=ArticleResponse)
async def create_article_api(req: CreateArticleRequest, request: Request, uid: str = Depends(verify_auth_token)):
    html_content = markdown_to_wechat_html(req.content, req.theme_id)
    art = article.create_article(req.title, req.content, html_content, req.theme_id, user_id=uid)

    base_url = str(request.base_url).rstrip("/")
    return _article_response(art, base_url)


@app.get("/api/articles/{article_id}", response_model=ArticleResponse)
async def get_article_api(article_id: str, request: Request, uid: str = Depends(verify_auth_token)):
    art = article.get_article(article_id, user_id=uid)
    if not art:
        raise HTTPException(status_code=404, detail="文章不存在")

    base_url = str(request.base_url).rstrip("/")
    return _article_response(art, base_url)


@app.put("/api/articles/{article_id}", response_model=ArticleResponse)
async def update_article_api(article_id: str, req: UpdateArticleRequest, request: Request, token: Optional[str] = None):
    user = current_user(request)
    if user:
        uid = user["id"]
    else:
        uid = verify_bearer_or_preview_token(request, token, article_id=article_id)

    existing = article.get_article(article_id, user_id=None if (user and user.get("role") == "admin") else uid)
    if not existing:
        raise HTTPException(status_code=404, detail="文章不存在")

    new_content = req.content if req.content is not None else existing.content
    new_theme = req.theme_id if req.theme_id is not None else existing.theme_id

    html_content = None
    if req.content is not None or req.theme_id is not None:
        html_content = markdown_to_wechat_html(new_content, new_theme)

    art = article.update_article(
        article_id, req.title, req.content, html_content, req.theme_id, user_id=existing.user_id
    )

    base_url = str(request.base_url).rstrip("/")
    return _article_response(art, base_url)


@app.get("/api/articles")
async def list_articles_api(request: Request, uid: str = Depends(verify_auth_token)):
    articles = article.list_articles(user_id=uid)
    base_url = str(request.base_url).rstrip("/")
    return [_article_response(art, base_url) for art in articles]


async def convert_local_images_to_wechat(html_content: str, base_url: str, wx_api: WechatAPI) -> str:
    """将本地图片 URL (/images/{user_id}/{file} 或 /images/{file}) 转换为微信图片 URL"""
    pattern = r'<img[^>]+src=["\']([^"\']*?/images/(?:([^/"\']+)/)?([^/"\']+))["\']'
    matches = list(re.finditer(pattern, html_content))
    if not matches:
        return html_content

    root = UPLOAD_DIR.resolve()
    result = html_content
    for match in reversed(matches):
        local_url = match.group(1)
        sub_dir = match.group(2)
        filename = match.group(3)
        file_path = (root / sub_dir / filename).resolve() if sub_dir else (root / filename).resolve()

        if not file_path.is_relative_to(root) or not file_path.is_file():
            continue

        image_data = file_path.read_bytes()
        try:
            wx_url = await wx_api.upload_image(image_data, filename)
            original = match.group(0)
            new_img = original.replace(local_url, wx_url)
            result = result[:match.start()] + new_img + result[match.end():]
        except Exception as e:
            print(f"上传图片失败 {filename}: {e}")

    return result


async def publish_article(article_id: str, user_id: Optional[str] = None) -> dict:
    """发布文章到对应租户微信公众号草稿箱（HTTP API 和 MCP 工具共用）"""
    art = article.get_article(article_id, user_id=user_id)
    if not art:
        return {"success": False, "draft_media_id": None, "message": "文章不存在"}

    uid = user_id or art.user_id or tenant.OWNER_ID
    if not tenant.is_user_active(uid):
        return {"success": False, "draft_media_id": None, "message": "账号已被停用"}

    wx_api = tenant.get_wechat_api(uid)
    if not wx_api:
        return {"success": False, "draft_media_id": None, "message": "微信 API 未配置，请先在设置页配置 AppID 与 AppSecret"}

    try:
        base_url = get_base_url()

        html_content = art.html_content
        ai_cover: Optional[bytes] = None
        try:
            ai = await imagegen.build(
                art.title, art.content, ref=article_id,
                upload_dir=UPLOAD_DIR, base=base_url, user_id=uid,
            )
            if ai.items or ai.cover_url:
                html_content = markdown_to_wechat_html(ai.markdown, art.theme_id)
                ai_cover = ai.cover_bytes
                print(f"AI 配图完成：封面 {bool(ai.cover_bytes)}，插图 {len(ai.items)} 张")
        except Exception as ai_err:
            print(f"AI 配图失败，按原文发布：{ai_err}")

        wx_html_content = await convert_local_images_to_wechat(html_content, base_url, wx_api)

        unsplash_key = os.getenv("UNSPLASH_ACCESS_KEY")
        if ai_cover:
            thumb_data = ai_cover
        elif unsplash_key:
            async with httpx.AsyncClient(timeout=15) as client:
                api_resp = await client.get(
                    "https://api.unsplash.com/photos/random",
                    headers={"Authorization": f"Client-ID {unsplash_key}"},
                    params={"orientation": "landscape", "query": "nature"},
                )
                raw_url = api_resp.json()["urls"]["raw"]
                cover_url = f"{raw_url}&w=900&h=383&fit=crop&q=80"
                img_resp = await client.get(cover_url, follow_redirects=True)
                thumb_data = img_resp.content
        else:
            default_cover = Path(__file__).parent.parent / "static" / "default_cover.png"
            thumb_data = default_cover.read_bytes()

        thumb_media_id = await wx_api.upload_thumb(thumb_data, "cover.jpg")

        tcfg = tenant.get_tenant_config(uid)
        author = ((tcfg.get("wechat") or {}).get("author") or "").strip() or (
            "程序杂念" if uid == tenant.OWNER_ID else ""
        )

        draft_media_id = await wx_api.create_draft(
            title=art.title,
            content=wx_html_content,
            thumb_media_id=thumb_media_id,
            author=author,
        )

        article.mark_published(article_id, draft_media_id, user_id=uid)
        return {"success": True, "draft_media_id": draft_media_id, "message": "已发布到草稿箱"}

    except Exception as e:
        import traceback
        traceback.print_exc()
        err_msg = f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__}: {repr(e)}"
        return {"success": False, "draft_media_id": None, "message": f"发布失败: {err_msg}"}


@app.post("/api/articles/{article_id}/publish", response_model=PublishResponse)
async def publish_article_api(article_id: str, request: Request, token: Optional[str] = None):
    user = current_user(request)
    if user:
        art = article.get_article(article_id, user_id=None if user.get("role") == "admin" else user["id"])
        if not art:
            raise HTTPException(status_code=404, detail="文章不存在")
        uid = art.user_id
    else:
        uid = verify_bearer_or_preview_token(request, token, article_id=article_id)

    result = await publish_article(article_id, user_id=uid)

    if not result["success"] and "微信 API 未配置" in result["message"]:
        raise HTTPException(status_code=500, detail=result["message"])
    if not result["success"] and result["message"] == "文章不存在":
        raise HTTPException(status_code=404, detail="文章不存在")

    return PublishResponse(**result)


# ========== 预览页面 ==========

@app.get("/preview/{article_id}", response_class=HTMLResponse)
async def preview_article(
    article_id: str,
    request: Request,
    token: Optional[str] = None,
    theme: Optional[str] = None,
):
    """文章预览页面：支持登录态或绑定文章的临时 token，并代理微信防盗链图片"""
    user = current_user(request)
    tok_info = get_preview_token_info(token) if token else None

    if token:
        if not tok_info:
            return HTMLResponse("<h1>401 未授权</h1><p>预览链接已过期或无效</p>", status_code=401)
        if tok_info.get("article_id") and tok_info["article_id"] != article_id:
            return HTMLResponse("<h1>403 无权访问</h1><p>预览链接与当前文章不匹配</p>", status_code=403)

    art = article.get_article(article_id)
    if not art:
        raise HTTPException(status_code=404, detail="文章不存在")

    if tok_info:
        if tok_info.get("user_id") and art.user_id and tok_info["user_id"] != art.user_id:
            return HTMLResponse("<h1>403 无权访问</h1><p>无权查看该租户文章</p>", status_code=403)
    elif user:
        if user.get("role") != "admin" and art.user_id != user["id"]:
            return HTMLResponse("<h1>403 无权访问</h1><p>无权查看该文章</p>", status_code=403)
    elif AUTH_TOKEN:
        return HTMLResponse(
            content="<h1>401 未授权</h1><p>请使用带 token 的链接访问或先登录</p>",
            status_code=401,
        )

    use_theme = theme or art.theme_id
    raw_html = markdown_to_html(art.content)
    preview_html = apply_theme_for_preview(raw_html, use_theme)
    preview_html = _rewrite_wx_images_for_preview(preview_html, token)

    theme_name = next(
        (t["name"] for t in get_available_themes() if t["id"] == use_theme),
        use_theme,
    )

    return templates.TemplateResponse(
        "preview.html",
        {
            "request": request,
            "article": art,
            "preview_html": preview_html,
            "theme_name": theme_name,
            "themes": get_available_themes(),
            "token": token or "",
        },
    )
