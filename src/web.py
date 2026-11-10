"""
Web 页面：登录、主页（文章列表）、设置

- /login、/auth/callback：Pocket ID OIDC 登录
- /、/settings：需要登录
- /preview/{id}：不受登录保护，继续用临时 token
"""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import article, config, notify, oidc
from .auth import create_preview_token
from .html_convert import list_themes

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


def current_user(request: Request) -> Optional[dict]:
    return oidc.read_session(request.cookies.get(oidc.SESSION_COOKIE))


def _theme_name(theme_id: str) -> str:
    for t in list_themes():
        if t["id"] == theme_id:
            return t["name"]
    return theme_id


def _status_badge(art) -> dict:
    if art.published_at:
        return {"label": "已推送", "tone": "ok"}
    return {"label": "草稿", "tone": "muted"}


# ---------- 登录 ----------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse("/", status_code=303)

    if not oidc.is_configured():
        return HTMLResponse(
            "<h1>OIDC 未配置</h1><p>请设置 OIDC_ISSUER / OIDC_CLIENT_ID / "
            "OIDC_CLIENT_SECRET / OIDC_REDIRECT_URI / SESSION_SECRET</p>",
            status_code=500,
        )

    try:
        authorize_url = await oidc.build_authorize_url()
    except Exception as e:
        logger.exception("生成授权地址失败")
        return HTMLResponse(f"<h1>登录不可用</h1><p>{e}</p>", status_code=500)

    return templates.TemplateResponse(
        "login.html", {"request": request, "authorize_url": authorize_url}
    )


@router.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    if error:
        return HTMLResponse(
            f"<h1>登录失败</h1><p>{error}: {error_description or ''}</p>"
            f"<p><a href='/login'>返回重试</a></p>",
            status_code=400,
        )
    if not code or not state:
        return HTMLResponse("<h1>登录失败</h1><p>缺少 code 或 state</p>", status_code=400)

    verifier = oidc.pop_state(state)
    if not verifier:
        return HTMLResponse("<h1>登录失败</h1><p>state 无效或已过期，请重试</p>", status_code=400)

    try:
        tokens = await oidc.exchange_code(code, verifier)
        user = await oidc.fetch_userinfo(tokens["access_token"])
    except Exception as e:
        logger.exception("OIDC 换取用户信息失败")
        return HTMLResponse(f"<h1>登录失败</h1><p>{e}</p>", status_code=400)

    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        oidc.SESSION_COOKIE,
        oidc.create_session(user),
        max_age=oidc.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )
    logger.info("用户登录: %s", user.get("email") or user.get("sub"))
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(oidc.SESSION_COOKIE, path="/")
    return resp


# ---------- 主页 ----------

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    articles = article.list_articles()
    token = create_preview_token()["token"]

    rows = []
    for art in articles:
        rows.append({
            "id": art.id,
            "title": art.title,
            "published_at": (art.published_at or art.created_at or "")[:16].replace("T", " "),
            "theme": _theme_name(art.theme_id),
            "status": _status_badge(art),
            "preview_url": f"/preview/{art.id}?token={token}",
        })

    return templates.TemplateResponse("home.html", {
        "request": request,
        "user": user,
        "rows": rows,
        "total": len(rows),
        "theme_name": _theme_name(config.get_theme()),
        "latest": rows[0]["published_at"] if rows else "—",
    })


# ---------- 设置 ----------

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: Optional[str] = None):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    cfg = config.load_config()
    themes = list_themes()
    articles = article.list_articles()
    token = create_preview_token()["token"]
    sample = articles[0] if articles else None
    current_theme = cfg.get("theme") or "default"

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "cfg": cfg,
        "themes": themes,
        "current_theme": current_theme,
        "saved": bool(saved),
        "sample": sample,
        "token": token,
        "notify_configured": bool(((cfg.get("notify") or {}).get("bark") or {}).get("key")),
    })


@router.post("/settings")
async def save_settings(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    form = await request.form()
    old = config.load_config()

    cfg = {
        "theme": (form.get("theme") or "default").strip(),
        "notify": {
            "enabled": form.get("notify_enabled") == "on",
            "bark": {
                "key": (form.get("bark_key") or "").strip(),
                "server": (form.get("bark_server") or "https://api.day.app").strip(),
                "group": (form.get("bark_group") or "公众号发布").strip(),
            },
            "events": {
                "start": form.get("ev_start") == "on",
                "success": form.get("ev_success") == "on",
                "error": form.get("ev_error") == "on",
            },
        },
    }
    # 设备 Key 留空 = 不改动（避免误清空）
    if not cfg["notify"]["bark"]["key"]:
        cfg["notify"]["bark"]["key"] = ((old.get("notify") or {}).get("bark") or {}).get("key", "")

    config.save_config(cfg)
    logger.info("设置已保存: theme=%s", cfg["theme"])
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/test-notify")
async def test_notify(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    result = await notify.send("发布台 · 测试通知", "如果你看到这条，说明 Bark 配好了。")
    return JSONResponse(result)
