"""
Web 页面：登录、主页（文章列表）、设置

- /login、/auth/callback：Pocket ID OIDC 登录
- /、/settings：需要登录
- /preview/{id}：不受登录保护，继续用临时 token
"""

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import article, config, imagegen, imagestyles, notify, notion_db, notion_webhook, oidc, password_auth, usage
from .auth import create_preview_token
from .html_convert import list_themes

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


def current_user(request: Request) -> Optional[dict]:
    return oidc.read_session(request.cookies.get(oidc.SESSION_COOKIE))


def _is_oidc_user(user: Optional[dict]) -> bool:
    return bool(user and (user.get("auth_method") or "oidc") == "oidc")


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return proto == "https" or request.url.scheme == "https"


def _human_age(seconds: int) -> str:
    if seconds < 10:
        return "刚刚"
    if seconds < 60:
        return f"{seconds} 秒前"
    mins = seconds // 60
    if mins < 60:
        return f"{mins} 分钟前"
    hours = mins // 60
    return f"{hours} 小时前"


def _webhook_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/notion/webhook"


_LOGIN_PW_ERRORS = {
    "invalid": "密码错误，请重试",
    "rate_limited": "尝试次数过多，请 5 分钟后再试",
    "disabled": "密码登录未开启",
}

_SETTINGS_PW_ERRORS = {
    "empty": "其他配置已保存，但开启密码登录前请先设置密码。",
    "short": f"其他配置已保存，但密码长度至少需要 {password_auth.MIN_PASSWORD_LENGTH} 位。",
}


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
async def login_page(request: Request, pw_err: Optional[str] = None):
    user = current_user(request)
    if user:
        return RedirectResponse("/", status_code=303)

    pw_enabled = password_auth.is_enabled()
    authorize_url = None
    oidc_error = None

    if not oidc.is_configured():
        if not pw_enabled:
            return HTMLResponse(
                "<h1>OIDC 未配置</h1><p>请设置 OIDC_ISSUER / OIDC_CLIENT_ID / "
                "OIDC_CLIENT_SECRET / OIDC_REDIRECT_URI / SESSION_SECRET</p>",
                status_code=500,
            )
        oidc_error = "Pocket ID 未配置"
    else:
        try:
            authorize_url = await oidc.build_authorize_url()
        except Exception as e:
            logger.exception("生成授权地址失败")
            if not pw_enabled:
                return HTMLResponse(f"<h1>登录不可用</h1><p>{e}</p>", status_code=500)
            oidc_error = "Pocket ID 暂时不可用，可使用密码登录"

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "authorize_url": authorize_url,
            "oidc_error": oidc_error,
            "password_login_enabled": pw_enabled,
            "pw_error": _LOGIN_PW_ERRORS.get(pw_err or ""),
        },
    )


@router.post("/login/password")
async def login_with_password(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse("/", status_code=303)

    if not oidc.has_session_secret():
        return HTMLResponse("<h1>登录不可用</h1><p>未配置 SESSION_SECRET</p>", status_code=500)

    cfg = config.load_config()
    if not password_auth.is_enabled(cfg):
        return RedirectResponse("/login?pw_err=disabled", status_code=303)

    ip = _client_ip(request)
    if password_auth.rate_limiter.is_blocked(ip):
        logger.warning("密码登录触发限流: ip=%s", ip)
        return RedirectResponse("/login?pw_err=rate_limited", status_code=303)

    form = await request.form()
    password = str(form.get("password") or "")
    stored_hash = ((cfg.get("password_login") or {}).get("password_hash")) or ""

    if not password_auth.verify_password(password, stored_hash):
        password_auth.rate_limiter.record_failure(ip)
        logger.warning("密码登录失败: ip=%s", ip)
        return RedirectResponse("/login?pw_err=invalid", status_code=303)

    password_auth.rate_limiter.reset(ip)
    pw_user = {
        "sub": "password",
        "name": "密码登录",
        "email": None,
        "picture": None,
        "auth_method": "password",
    }
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        oidc.SESSION_COOKIE,
        oidc.create_session(pw_user),
        max_age=oidc.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )
    logger.info("用户通过固定密码登录: ip=%s", ip)
    return resp


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
        user["auth_method"] = "oidc"
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
async def settings_page(
    request: Request,
    saved: Optional[str] = None,
    pw_err: Optional[str] = None,
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    cfg = config.load_config()
    notify_configured = bool(((cfg.get("notify") or {}).get("bark") or {}).get("key"))
    ai_key_configured = bool((cfg.get("ai_images") or {}).get("api_key"))
    # 密钥只允许设置/更新，绝不回显到页面 HTML
    if isinstance(cfg.get("notify"), dict) and isinstance(cfg["notify"].get("bark"), dict):
        cfg["notify"]["bark"]["key"] = ""
    if isinstance(cfg.get("ai_images"), dict):
        cfg["ai_images"]["api_key"] = ""

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
        "notify_configured": notify_configured,
        "ai_key_configured": ai_key_configured,
        "pw_enabled": password_auth.is_enabled(cfg),
        "pw_has_password": password_auth.has_password(cfg),
        "can_manage_password": _is_oidc_user(user),
        "pw_error": _SETTINGS_PW_ERRORS.get(pw_err or ""),
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

    # ---- AI 配图 ----
    ai_old = old.get("ai_images") or {}
    try:
        max_width = int(form.get("ai_max_width") or ai_old.get("max_width") or 1200)
    except (TypeError, ValueError):
        max_width = 1200
    cfg["ai_images"] = {
        "enabled": form.get("ai_enabled") == "on",
        "base_url": (form.get("ai_base_url") or "").strip(),
        "api_key": (form.get("ai_api_key") or "").strip(),
        "text_model": (form.get("ai_text_model") or "").strip(),
        "image_model": (form.get("ai_image_model") or "").strip(),
        "image_api": (form.get("ai_image_api") or "auto").strip() or "auto",
        "style_mode": (form.get("ai_style_mode") or "auto").strip() or "auto",
        "style": (form.get("ai_style") or "").strip(),
        "write_to_notion": form.get("ai_write_notion") == "on",
        "max_width": max_width,
        "cover_size": (form.get("ai_cover_size") or "1536x1024").strip(),
        "illustration_size": (form.get("ai_illustration_size") or "1536x1024").strip(),
    }
    # API Key 留空 = 沿用旧的
    if not cfg["ai_images"]["api_key"]:
        cfg["ai_images"]["api_key"] = ai_old.get("api_key", "")

    # ---- 固定密码登录（仅 Pocket ID 登录的会话允许修改）----
    pw_old = old.get("password_login") or {"enabled": False, "password_hash": ""}
    pw_err = None
    if _is_oidc_user(user):
        want_enabled = form.get("pw_login_enabled") == "on"
        new_pw = str(form.get("pw_login_password") or "")
        pw_hash = pw_old.get("password_hash") or ""

        if new_pw:
            if len(new_pw) < password_auth.MIN_PASSWORD_LENGTH:
                pw_err = "short"
                cfg["password_login"] = dict(pw_old)
            else:
                pw_hash = password_auth.hash_password(new_pw)

        if pw_err is None:
            if want_enabled and not pw_hash:
                pw_err = "empty"
                cfg["password_login"] = {"enabled": False, "password_hash": ""}
            else:
                cfg["password_login"] = {"enabled": want_enabled, "password_hash": pw_hash}
    else:
        cfg["password_login"] = dict(pw_old)

    config.save_config(cfg)
    logger.info(
        "设置已保存: theme=%s, password_login=%s",
        cfg["theme"],
        cfg["password_login"]["enabled"],
    )
    if pw_err:
        return RedirectResponse(f"/settings?saved=1&pw_err={pw_err}", status_code=303)
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/test-notify")
async def test_notify(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    result = await notify.send("发布台 · 测试通知", "如果你看到这条，说明 Bark 配好了。")
    return JSONResponse(result)


@router.post("/settings/test-ai")
async def test_ai(request: Request):
    """测试模型接口连通性：只拉模型列表，不烧生图额度。"""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    result = await imagegen.test_connection()
    return JSONResponse(result)


# ---------- 用量 ----------

@router.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    return templates.TemplateResponse("usage.html", {
        "request": request,
        "user": user,
        "totals": usage.totals(),
        "steps": usage.by_step(),
        "articles": usage.by_article(30),
        "recent": usage.recent(50),
        "styles_used": imagestyles.recent_log(12),
        "ai_on": bool(((config.load_config().get("ai_images") or {}).get("enabled"))),
    })


# ---------- 数据源（Notion Database） ----------

@router.get("/source", response_class=HTMLResponse)
async def source_page(request: Request, refresh: Optional[str] = None):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    snap = await notion_db.get_database_snapshot(force_refresh=bool(refresh))
    source_map = article.list_notion_source_map()
    token = create_preview_token()["token"]

    items = []
    synced_count = 0
    for it in snap.get("items") or []:
        key = notion_db.normalize_id(it.get("id"))
        local = source_map.get(key)
        if local:
            synced_count += 1
        items.append({
            **it,
            "synced": bool(local),
            "local_id": local["id"] if local else "",
            "local_updated_at": (local["updated_at"][:16].replace("T", " ") if local and local.get("updated_at") else ""),
            "preview_url": f"/preview/{local['id']}?token={token}" if local else "",
        })

    wh = notion_webhook.get_runtime_stats()
    cache = snap.get("cache") or {}
    age_text = _human_age(int(cache.get("age_seconds") or 0)) if cache.get("fetched_at") else "尚未同步"

    return templates.TemplateResponse("source.html", {
        "request": request,
        "user": user,
        "snap": snap,
        "db": snap.get("database") or {},
        "stats": snap.get("stats") or {},
        "cache": cache,
        "age_text": age_text,
        "items": items,
        "synced_count": synced_count,
        "wh": wh,
        "webhook_url": _webhook_url(request),
    })


@router.post("/source/refresh")
async def source_refresh(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    snap = await notion_db.get_database_snapshot(force_refresh=True)
    return JSONResponse({
        "ok": not bool(snap.get("error")),
        "total": (snap.get("stats") or {}).get("total", 0),
        "fetched_at": (snap.get("cache") or {}).get("fetched_at", ""),
        "error": snap.get("error"),
    })


# ---------- 流程拓扑 ----------

@router.get("/flow", response_class=HTMLResponse)
async def flow_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    snap = await notion_db.get_database_snapshot(force_refresh=False)
    wh = notion_webhook.get_runtime_stats()
    cfg = config.load_config()
    articles = article.list_articles()
    totals = usage.totals()

    ai_cfg = cfg.get("ai_images") or {}
    notify_cfg = cfg.get("notify") or {}
    bark_cfg = notify_cfg.get("bark") or {}
    notify_events = notify_cfg.get("events") or {}

    ai_enabled = bool(ai_cfg.get("enabled"))
    notify_enabled = bool(notify_cfg.get("enabled") and bark_cfg.get("key"))
    wechat_ready = bool(wh.get("wechat_configured"))
    notion_ready = bool(snap.get("configured") and not snap.get("error"))
    write_back_enabled = bool(ai_enabled and ai_cfg.get("write_to_notion", True))

    published_count = sum(1 for a in articles if a.published_at)
    total_articles = len(articles)
    latest_push = (
        (articles[0].published_at or articles[0].updated_at or "")[:16].replace("T", " ")
        if articles else "—"
    )
    notion_stats = snap.get("stats") or {}
    notion_total = int(notion_stats.get("total") or 0)
    notion_status_counts = notion_stats.get("status_counts") or {}
    notion_failed = int(notion_status_counts.get("发布失败") or 0)
    notion_published = int(notion_status_counts.get("已发布") or 0)
    sync_pct = round(min(100.0, (total_articles * 100.0 / notion_total)), 1) if notion_total else 0.0

    downstream_ready = sum([
        1 if wechat_ready else 0,
        1 if notion_ready else 0,
        1 if ai_enabled else 0,
        1 if notify_enabled else 0,
        1,  # 本地发布台与预览始终可用
    ])

    return templates.TemplateResponse("flow.html", {
        "request": request,
        "user": user,
        "snap": snap,
        "db": snap.get("database") or {},
        "wh": wh,
        "webhook_url": _webhook_url(request),
        "theme_name": _theme_name(cfg.get("theme") or "default"),
        "ai_cfg": ai_cfg,
        "ai_enabled": ai_enabled,
        "write_back_enabled": write_back_enabled,
        "notify_enabled": notify_enabled,
        "notify_events": notify_events,
        "wechat_ready": wechat_ready,
        "notion_ready": notion_ready,
        "auth_token_on": bool(os.getenv("AUTH_TOKEN")),
        "total_articles": total_articles,
        "published_count": published_count,
        "latest_push": latest_push,
        "notion_total": notion_total,
        "notion_published": notion_published,
        "notion_failed": notion_failed,
        "sync_pct": sync_pct,
        "totals": totals,
        "downstream_ready": downstream_ready,
    })
