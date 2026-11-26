"""
Web 页面：登录、注册、主页（文章列表）、数据源、流程拓扑、用量、设置、管理员后台

- /login、/register、/auth/callback：多租户账号密码登录/注册 + 管理员 Pocket ID OIDC
- /、/source、/flow、/usage、/settings：需登录且按租户严格隔离
- /admin：仅管理员可见（用户权限管控 + 全站统一 AI 绘图引擎与注册开关）
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import (
    article,
    config,
    imagegen,
    imagestyles,
    notify,
    notion_db,
    notion_webhook,
    oidc,
    password_auth,
    tenant,
    usage,
)
from .auth import create_preview_token
from .html_convert import list_themes

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")


def current_user(request: Request) -> Optional[dict]:
    """读取签名会话并实时校验数据库账号状态（停用即刻失效）"""
    sess = oidc.read_session(request.cookies.get(oidc.SESSION_COOKIE))
    if not sess:
        return None
    uid = sess.get("uid") or tenant.OWNER_ID
    u = tenant.get_user_by_id(uid)
    if not u or u.get("status") != "active":
        return None
    return {
        "id": u["id"],
        "uid": u["id"],
        "username": u["username"],
        "name": u["display_name"] or sess.get("name") or u["username"],
        "display_name": u["display_name"],
        "role": u["role"],
        "status": u["status"],
        "email": sess.get("email"),
        "picture": sess.get("picture"),
        "auth_method": sess.get("auth_method") or "password",
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


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return proto == "https" or request.url.scheme == "https"


def _base_url(request: Request) -> str:
    env_base = os.getenv("BASE_URL", "").strip()
    if env_base:
        return env_base.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


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


def _user_webhook_url(request: Request, u: dict) -> str:
    base = _base_url(request)
    wt = u.get("webhook_token") or ""
    if wt:
        return f"{base}/notion/webhook/{wt}"
    if u.get("id") == tenant.OWNER_ID:
        return f"{base}/notion/webhook"
    return ""


def _user_mcp_url(request: Request, u: dict) -> str:
    base = _base_url(request)
    mt = u.get("mcp_token") or ""
    return f"{base}/mcp/{mt}" if mt else ""


def _set_session_cookie(resp: RedirectResponse, request: Request, user: dict, auth_method: str, extra: Optional[dict] = None) -> None:
    payload = {
        "id": user["id"],
        "uid": user["id"],
        "username": user["username"],
        "name": user["display_name"] or user["username"],
        "role": user["role"],
        "auth_method": auth_method,
    }
    if extra:
        payload.update(extra)
    resp.set_cookie(
        oidc.SESSION_COOKIE,
        oidc.create_session(payload),
        max_age=oidc.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )


_LOGIN_PW_ERRORS = {
    "invalid": "账号或密码错误，请重试",
    "disabled_user": "该账号已被管理员停用，请联系管理员",
    "rate_limited": "尝试次数过多，请 5 分钟后再试",
}

_REGISTER_ERRORS = {
    "closed": "当前已关闭新用户注册，请联系管理员",
    "invalid_username": "用户名需为 3~64 位小写字母、数字或 . _ - @",
    "short_password": f"密码长度至少需要 {password_auth.MIN_PASSWORD_LENGTH} 位",
    "mismatch": "两次输入的密码不一致",
    "username_taken": "该用户名已被注册，请换一个",
}

_SETTINGS_PW_ERRORS = {
    "short": f"其他配置已保存，但新密码长度至少需要 {password_auth.MIN_PASSWORD_LENGTH} 位。",
}


# ---------- 登录 & 注册 ----------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, pw_err: Optional[str] = None, username: Optional[str] = None):
    user = current_user(request)
    if user:
        return RedirectResponse("/", status_code=303)

    tenant.bootstrap_owner()
    authorize_url = None
    oidc_error = None

    if oidc.is_configured():
        try:
            authorize_url = await oidc.build_authorize_url()
        except Exception as e:
            logger.exception("生成授权地址失败")
            oidc_error = "Pocket ID 暂时不可用，请使用账号密码登录"

    gcfg = config.load_config()
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "authorize_url": authorize_url,
            "oidc_error": oidc_error,
            "allow_registration": bool(gcfg.get("allow_registration", True)),
            "pw_error": _LOGIN_PW_ERRORS.get(pw_err or ""),
            "username_prefill": (username or "").strip(),
        },
    )


@router.post("/login/password")
async def login_with_password(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse("/", status_code=303)

    if not oidc.has_session_secret():
        return HTMLResponse("<h1>登录不可用</h1><p>未配置 SESSION_SECRET</p>", status_code=500)

    ip = _client_ip(request)
    if password_auth.rate_limiter.is_blocked(ip):
        logger.warning("密码登录触发限流: ip=%s", ip)
        return RedirectResponse("/login?pw_err=rate_limited", status_code=303)

    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")

    matched_user, err = tenant.authenticate_user(username, password)
    if err == "disabled":
        logger.warning("已停用账号尝试登录: user=%s ip=%s", username, ip)
        return RedirectResponse(f"/login?pw_err=disabled_user&username={username}", status_code=303)
    if not matched_user:
        password_auth.rate_limiter.record_failure(ip)
        logger.warning("密码登录失败: user=%s ip=%s", username or "admin", ip)
        return RedirectResponse(f"/login?pw_err=invalid&username={username}", status_code=303)

    password_auth.rate_limiter.reset(ip)
    resp = RedirectResponse("/", status_code=303)
    _set_session_cookie(resp, request, matched_user, "password")
    logger.info("用户账号密码登录: user=%s ip=%s", matched_user["username"], ip)
    return resp


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, err: Optional[str] = None, username: Optional[str] = None):
    if current_user(request):
        return RedirectResponse("/", status_code=303)

    gcfg = config.load_config()
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "allow_registration": bool(gcfg.get("allow_registration", True)),
            "error": _REGISTER_ERRORS.get(err or ""),
            "username_prefill": (username or "").strip(),
        },
    )


@router.post("/register")
async def register_submit(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)

    if not oidc.has_session_secret():
        return HTMLResponse("<h1>注册不可用</h1><p>未配置 SESSION_SECRET</p>", status_code=500)

    gcfg = config.load_config()
    if not gcfg.get("allow_registration", True):
        return RedirectResponse("/register?err=closed", status_code=303)

    form = await request.form()
    username = str(form.get("username") or "").strip()
    display_name = str(form.get("display_name") or "").strip()
    password = str(form.get("password") or "")
    confirm = str(form.get("confirm_password") or "")

    if password != confirm:
        return RedirectResponse(f"/register?err=mismatch&username={username}", status_code=303)

    user, err = tenant.register_user(username, password, display_name)
    if err or not user:
        return RedirectResponse(f"/register?err={err or 'invalid_username'}&username={username}", status_code=303)

    resp = RedirectResponse("/settings?welcome=1", status_code=303)
    _set_session_cookie(resp, request, user, "password")
    logger.info("新用户注册成功: id=%s username=%s", user["id"], user["username"])
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
        oidc_user = await oidc.fetch_userinfo(tokens["access_token"])
    except Exception as e:
        logger.exception("OIDC 换取用户信息失败")
        return HTMLResponse(f"<h1>登录失败</h1><p>{e}</p>", status_code=400)

    bound_user, err = tenant.bind_or_get_oidc_user(oidc_user)
    if err == "disabled":
        return HTMLResponse("<h1>账号已停用</h1><p>请联系管理员恢复权限</p>", status_code=403)
    if not bound_user:
        return HTMLResponse("<h1>登录失败</h1><p>未找到可用账号</p>", status_code=400)

    resp = RedirectResponse("/", status_code=303)
    _set_session_cookie(
        resp,
        request,
        bound_user,
        "oidc",
        extra={
            "sub": oidc_user.get("sub"),
            "email": oidc_user.get("email"),
            "picture": oidc_user.get("picture"),
        },
    )
    logger.info("Pocket ID 登录: %s -> %s", oidc_user.get("email") or oidc_user.get("sub"), bound_user["id"])
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

    uid = user["id"]
    u_row = tenant.get_user_by_id(uid)
    tcfg = (u_row or {}).get("config") or tenant.TENANT_DEFAULTS
    articles = article.list_articles(user_id=uid)
    token = create_preview_token(user_id=uid)["token"]

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

    wx = tcfg.get("wechat") or {}
    return templates.TemplateResponse("home.html", {
        "request": request,
        "user": user,
        "rows": rows,
        "total": len(rows),
        "theme_name": _theme_name(tcfg.get("theme") or "default"),
        "latest": rows[0]["published_at"] if rows else "—",
        "wechat_ready": bool(wx.get("appid") and wx.get("appsecret")),
        "seed_ready": bool((u_row or {}).get("mcp_token")),
    })


# ---------- 设置 ----------

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    saved: Optional[str] = None,
    welcome: Optional[str] = None,
    seed_reset: Optional[str] = None,
    seed_created: Optional[str] = None,
    pw_err: Optional[str] = None,
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    uid = user["id"]
    u_row = tenant.get_user_by_id(uid) or {}
    cfg = json.loads(json.dumps(u_row.get("config") or tenant.TENANT_DEFAULTS))

    wx = cfg.get("wechat") or {}
    nt = cfg.get("notion") or {}
    bark = (cfg.get("notify") or {}).get("bark") or {}

    wx_secret_set = bool(wx.get("appsecret"))
    notion_token_set = bool(nt.get("token"))
    notion_wh_secret_set = bool(nt.get("webhook_secret"))
    notify_configured = bool(bark.get("key"))

    # 所有敏感密钥只允许设置/更新，绝不回显到 HTML
    if isinstance(wx, dict):
        wx["appsecret"] = ""
    if isinstance(nt, dict):
        nt["token"] = ""
        nt["webhook_secret"] = ""
    if isinstance(bark, dict):
        bark["key"] = ""

    mcp_url = _user_mcp_url(request, u_row)
    webhook_url = _user_webhook_url(request, u_row)
    mcp_snippet = json.dumps(
        {
            "mcpServers": {
                "wechat-publisher": {
                    "url": mcp_url or f"{_base_url(request)}/mcp/<配置微信后自动生成>",
                }
            }
        },
        ensure_ascii=False,
        indent=2,
    )

    platform_ai = config.load_config().get("ai_images") or {}
    platform_ai_ready = bool(
        platform_ai.get("base_url")
        and platform_ai.get("api_key")
        and platform_ai.get("text_model")
        and platform_ai.get("image_model")
    )

    themes = list_themes()
    articles = article.list_articles(user_id=uid)
    sample = articles[0] if articles else None
    token = create_preview_token(user_id=uid, article_id=sample.id if sample else None)["token"]
    current_theme = cfg.get("theme") or "default"

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "user": user,
        "u": u_row,
        "cfg": cfg,
        "themes": themes,
        "current_theme": current_theme,
        "saved": bool(saved),
        "welcome": bool(welcome),
        "seed_reset": bool(seed_reset),
        "seed_created": bool(seed_created),
        "sample": sample,
        "token": token,
        "wx_ready": bool(wx.get("appid") and wx_secret_set),
        "wx_secret_set": wx_secret_set,
        "notion_token_set": notion_token_set,
        "notion_wh_secret_set": notion_wh_secret_set,
        "notify_configured": notify_configured,
        "mcp_url": mcp_url,
        "webhook_url": webhook_url,
        "mcp_snippet": mcp_snippet,
        "server_ip": os.getenv("SERVER_PUBLIC_IP", "161.153.84.30"),
        "platform_ai_ready": platform_ai_ready,
        "pw_error": _SETTINGS_PW_ERRORS.get(pw_err or ""),
    })


@router.post("/settings")
async def save_settings(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    uid = user["id"]
    u_before = tenant.get_user_by_id(uid) or {}
    had_seed = bool(u_before.get("mcp_token"))
    old = u_before.get("config") or tenant.TENANT_DEFAULTS

    form = await request.form()

    wx_old = old.get("wechat") or {}
    nt_old = old.get("notion") or {}
    bark_old = (old.get("notify") or {}).get("bark") or {}
    ai_old = old.get("ai_images") or {}

    raw_min = (form.get("ai_skip_min_images") or "").strip()
    min_val = raw_min if raw_min else ai_old.get("skip_min_images")
    if min_val is None or min_val == "":
        skip_min_images = 2
    else:
        try:
            skip_min_images = max(1, int(min_val))
        except (TypeError, ValueError):
            skip_min_images = 2

    cfg = {
        "theme": (form.get("theme") or "default").strip(),
        "wechat": {
            "appid": (form.get("wx_appid") or "").strip(),
            "appsecret": (form.get("wx_appsecret") or "").strip() or (wx_old.get("appsecret") or ""),
            "author": (form.get("wx_author") or "").strip(),
        },
        "notion": {
            "token": (form.get("notion_token") or "").strip() or (nt_old.get("token") or ""),
            "db_id": (form.get("notion_db_id") or "").strip(),
            "webhook_secret": (form.get("notion_webhook_secret") or "").strip() or (nt_old.get("webhook_secret") or ""),
        },
        "notify": {
            "enabled": form.get("notify_enabled") == "on",
            "bark": {
                "key": (form.get("bark_key") or "").strip() or (bark_old.get("key") or ""),
                "server": (form.get("bark_server") or "https://api.day.app").strip(),
                "group": (form.get("bark_group") or "公众号发布").strip(),
            },
            "events": {
                "start": form.get("ev_start") == "on",
                "success": form.get("ev_success") == "on",
                "error": form.get("ev_error") == "on",
            },
        },
        "ai_images": {
            "enabled": form.get("ai_enabled") == "on",
            "style_mode": (form.get("ai_style_mode") or "auto").strip() or "auto",
            "style": (form.get("ai_style") or "").strip(),
            "write_to_notion": form.get("ai_write_notion") == "on",
            "skip_if_has_images": form.get("ai_skip_if_has_images") == "on",
            "skip_min_images": skip_min_images,
        },
    }

    tenant.save_tenant_config(uid, cfg)
    notion_db.clear_cache(uid)

    u_after = tenant.get_user_by_id(uid) or {}
    seed_created = (not had_seed) and bool(u_after.get("mcp_token"))

    pw_err = None
    new_pw = str(form.get("new_password") or "")
    if new_pw:
        _, pw_err = tenant.update_user_password(uid, new_pw)

    logger.info("租户设置已保存: user=%s theme=%s seed_created=%s", uid, cfg["theme"], seed_created)
    params = ["saved=1"]
    if seed_created:
        params.append("seed_created=1")
    if pw_err:
        params.append(f"pw_err={pw_err}")
    return RedirectResponse("/settings?" + "&".join(params), status_code=303)


@router.post("/settings/reset-seed")
async def reset_seed(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    u = tenant.regenerate_seed(user["id"])
    if not u or not u.get("mcp_token"):
        return JSONResponse({"ok": False, "reason": "请先配置并保存公众号 AppID 和 AppSecret"}, status_code=400)
    return JSONResponse({
        "ok": True,
        "mcp_url": _user_mcp_url(request, u),
        "webhook_url": _user_webhook_url(request, u),
    })


@router.post("/settings/test-wechat")
async def test_wechat(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    wx = tenant.get_wechat_api(user["id"])
    if not wx:
        return JSONResponse({"ok": False, "reason": "请先填写 AppID 与 AppSecret 并点击保存"})

    try:
        await wx.get_access_token()
        return JSONResponse({"ok": True, "message": "公众号凭证校验通过，可以正常推送草稿箱"})
    except Exception as e:
        msg = str(e)
        if "40164" in msg:
            tip = f"服务器 IP 未加入公众号白名单，请把 {os.getenv('SERVER_PUBLIC_IP', '161.153.84.30')} 加到公众号后台 IP 白名单"
        elif "40125" in msg or "40001" in msg:
            tip = "AppSecret 不正确，请核对公众号开发者密钥并重新保存"
        elif "40013" in msg:
            tip = "AppID 无效，请核对公众号开发者 ID"
        else:
            tip = msg[:160]
        return JSONResponse({"ok": False, "reason": tip})


@router.post("/settings/test-notify")
async def test_notify(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    result = await notify.send("发布台 · 测试通知", "如果你看到这条，说明 Bark 配好了。", user_id=user["id"])
    return JSONResponse(result)


@router.post("/settings/test-ai")
async def test_ai(request: Request):
    """测试平台统一模型接口连通性（仅管理员可调用）"""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)
    if user.get("role") != "admin":
        return JSONResponse({"ok": False, "reason": "仅管理员可测试平台模型接口"}, status_code=403)

    result = await imagegen.test_connection(config.load_config().get("ai_images") or {})
    return JSONResponse(result)


# ---------- 用量 ----------

@router.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    uid = user["id"]
    ai_cfg = tenant.get_effective_ai_config(uid)
    return templates.TemplateResponse("usage.html", {
        "request": request,
        "user": user,
        "totals": usage.totals(user_id=uid),
        "steps": usage.by_step(user_id=uid),
        "articles": usage.by_article(30, user_id=uid),
        "recent": usage.recent(50, user_id=uid),
        "styles_used": imagestyles.recent_log(12, user_id=uid),
        "ai_on": bool(ai_cfg.get("enabled")),
    })


# ---------- 数据源（Notion Database） ----------

@router.get("/source", response_class=HTMLResponse)
async def source_page(request: Request, refresh: Optional[str] = None):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    uid = user["id"]
    u_row = tenant.get_user_by_id(uid) or {}
    snap = await notion_db.get_database_snapshot(user_id=uid, force_refresh=bool(refresh))
    source_map = article.list_notion_source_map(user_id=uid)
    token = create_preview_token(user_id=uid)["token"]

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

    wh = notion_webhook.get_runtime_stats(uid)
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
        "webhook_url": _user_webhook_url(request, u_row),
    })


@router.post("/source/refresh")
async def source_refresh(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    snap = await notion_db.get_database_snapshot(user_id=user["id"], force_refresh=True)
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

    uid = user["id"]
    u_row = tenant.get_user_by_id(uid) or {}
    snap = await notion_db.get_database_snapshot(user_id=uid, force_refresh=False)
    wh = notion_webhook.get_runtime_stats(uid)
    cfg = u_row.get("config") or tenant.TENANT_DEFAULTS
    ai_cfg = tenant.get_effective_ai_config(uid)
    articles = article.list_articles(user_id=uid)
    totals = usage.totals(user_id=uid)

    notify_cfg = cfg.get("notify") or {}
    bark_cfg = notify_cfg.get("bark") or {}
    notify_events = notify_cfg.get("events") or {}

    ai_enabled = bool(ai_cfg.get("enabled") and ai_cfg.get("base_url") and ai_cfg.get("api_key"))
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
        1,
    ])

    return templates.TemplateResponse("flow.html", {
        "request": request,
        "user": user,
        "snap": snap,
        "db": snap.get("database") or {},
        "wh": wh,
        "webhook_url": _user_webhook_url(request, u_row),
        "mcp_url": _user_mcp_url(request, u_row),
        "theme_name": _theme_name(cfg.get("theme") or "default"),
        "ai_cfg": ai_cfg,
        "ai_enabled": ai_enabled,
        "write_back_enabled": write_back_enabled,
        "notify_enabled": notify_enabled,
        "notify_events": notify_events,
        "wechat_ready": wechat_ready,
        "notion_ready": notion_ready,
        "mcp_ready": bool(u_row.get("mcp_token")),
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


# ---------- 管理员后台 ----------

@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, saved: Optional[str] = None, msg: Optional[str] = None):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("role") != "admin":
        return HTMLResponse("<h1>403 无权访问</h1><p>仅管理员可访问平台后台</p>", status_code=403)

    users = tenant.list_users_with_stats()
    gcfg = json.loads(json.dumps(config.load_config()))
    ai_cfg = gcfg.get("ai_images") or {}
    ai_key_set = bool(ai_cfg.get("api_key"))
    if isinstance(ai_cfg, dict):
        ai_cfg["api_key"] = ""

    site_totals = usage.totals(user_id=None)
    total_articles = len(article.list_articles(user_id=None))
    active_users = sum(1 for u in users if u["status"] == "active")

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "user": user,
        "users": users,
        "total_users": len(users),
        "active_users": active_users,
        "total_articles": total_articles,
        "site_totals": site_totals,
        "gcfg": gcfg,
        "ai_key_set": ai_key_set,
        "saved": bool(saved),
        "msg": msg,
    })


@router.post("/admin/users/{target_uid}/status")
async def admin_toggle_user_status(target_uid: str, request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("role") != "admin":
        return HTMLResponse("<h1>403 无权访问</h1>", status_code=403)

    form = await request.form()
    new_status = str(form.get("status") or "").strip()
    ok, err = tenant.set_user_status(target_uid, new_status)
    if not ok:
        return RedirectResponse(f"/admin?msg={err or '操作失败'}", status_code=303)
    tip = "已启用该用户" if new_status == "active" else "已停用该用户（其会话与专属链接立即失效）"
    return RedirectResponse(f"/admin?msg={tip}", status_code=303)


@router.post("/admin/platform")
async def admin_save_platform(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if user.get("role") != "admin":
        return HTMLResponse("<h1>403 无权访问</h1>", status_code=403)

    form = await request.form()
    old = config.load_config()
    ai_old = old.get("ai_images") or {}
    try:
        max_width = int(form.get("ai_max_width") or ai_old.get("max_width") or 1200)
    except (TypeError, ValueError):
        max_width = 1200

    new_cfg = dict(old)
    new_cfg["allow_registration"] = form.get("allow_registration") == "on"
    new_cfg["ai_images"] = {
        **ai_old,
        "base_url": (form.get("ai_base_url") or "").strip(),
        "api_key": (form.get("ai_api_key") or "").strip() or (ai_old.get("api_key") or ""),
        "text_model": (form.get("ai_text_model") or "").strip(),
        "image_model": (form.get("ai_image_model") or "").strip(),
        "image_api": (form.get("ai_image_api") or "auto").strip() or "auto",
        "max_width": max_width,
        "cover_size": (form.get("ai_cover_size") or "1536x1024").strip(),
        "illustration_size": (form.get("ai_illustration_size") or "1536x1024").strip(),
    }
    config.save_config(new_cfg)
    logger.info("平台全局配置已更新: allow_registration=%s", new_cfg["allow_registration"])
    return RedirectResponse("/admin?saved=1", status_code=303)
