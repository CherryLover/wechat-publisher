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
from urllib.parse import quote

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
from .html_convert import apply_theme_for_preview, list_themes, markdown_to_html
from .notion_source import NotionSource
from .wechat import WechatAPI

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

_ONBOARDING_STEPS = [
    {"id": "wechat", "index": 1, "title": "微信公众号", "required": True, "desc": "验证 AppID 与 AppSecret 连通性"},
    {"id": "notion", "index": 2, "title": "Notion 数据源", "required": False, "desc": "绑定文档数据库与同步触发"},
    {"id": "ai", "index": 3, "title": "AI 智能配图", "required": False, "desc": "按需开启自动封面与正文插图"},
    {"id": "theme", "index": 4, "title": "排版主题", "required": False, "desc": "实时预览公众号文章排版效果"},
    {"id": "notify", "index": 5, "title": "Bark 通知", "required": False, "desc": "发布进度与异常即时推送"},
]
_STEP_ALIASES = {
    "1": "wechat", "wechat": "wechat",
    "2": "notion", "notion": "notion",
    "3": "ai", "ai": "ai",
    "4": "theme", "theme": "theme",
    "5": "notify", "notify": "notify",
}

_DEMO_ARTICLE_MD = """# 把每周灵感冒泡成一篇好文章

当你把笔记整理进 Notion，发布台会在后台完成主题排版、图片转存与草稿推送，你只需要专注于表达本身。

> 好排版不是喧宾夺主，而是让读者舒服地把整篇文章读完。

## 为什么选择自动化工作流

1. **一次写作，随时同步**：在 Notion 修改状态为“待发布”，几秒内自动生成公众号草稿。
2. **多主题即时切换**：同一份 Markdown 文稿，可在简洁、暖刊或极客风之间自由预览与发布。
3. **关键凭据加密保管**：AppSecret 与 Notion Token 均以 AES-256-GCM 密文形式独立存储。

### 排版细节预览

正文中的 `行内代码`、加粗重点与引用段落会随左侧选中的主题实时变化；如果你开启了 **AI 配图**，未配图的文章还会自动生成匹配语境的封面与插图。

---

*以上为实时排版示例文案，你可以在左侧切换不同主题风格即时查看效果。*
"""


def _post_login_target(user_id: str, welcome: bool = False) -> str:
    if tenant.needs_onboarding(user_id):
        return "/onboarding?welcome=1" if welcome else "/onboarding"
    return "/"


async def _verify_wechat_credentials(appid: str, appsecret: str) -> tuple[bool, str]:
    """实时调用微信 API 校验 AppID 与 AppSecret 是否可用"""
    aid = (appid or "").strip()
    sec = (appsecret or "").strip()
    if not (aid and sec):
        return False, "请先填写 AppID 与 AppSecret"
    try:
        wx = WechatAPI(aid, sec)
        await wx.get_access_token()
        return True, "公众号凭证校验通过，可以正常推送草稿箱"
    except Exception as e:
        msg = str(e)
        if "40164" in msg:
            tip = f"服务器 IP 未加入公众号白名单，请把 {os.getenv('SERVER_PUBLIC_IP', '161.153.84.30')} 加到公众号后台 IP 白名单"
        elif "40125" in msg or "40001" in msg:
            tip = "AppSecret 不正确，请核对公众号开发者密钥"
        elif "40013" in msg:
            tip = "AppID 无效，请核对公众号开发者 ID"
        else:
            tip = msg[:160]
        return False, tip


async def _read_payload(request: Request) -> tuple[dict, bool]:
    """兼容 JSON 与 Form 提交，返回 (payload_dict, is_json)"""
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            data = await request.json()
            return (data if isinstance(data, dict) else {}), True
        except Exception:
            return {}, True
    form = await request.form()
    return {k: v for k, v in form.items()}, False


# ---------- 登录 & 注册 ----------

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, pw_err: Optional[str] = None, username: Optional[str] = None):
    user = current_user(request)
    if user:
        return RedirectResponse(_post_login_target(user["id"]), status_code=303)

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
    resp = RedirectResponse(_post_login_target(matched_user["id"]), status_code=303)
    _set_session_cookie(resp, request, matched_user, "password")
    logger.info("用户账号密码登录: user=%s ip=%s", matched_user["username"], ip)
    return resp


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, err: Optional[str] = None, username: Optional[str] = None):
    user = current_user(request)
    if user:
        return RedirectResponse(_post_login_target(user["id"]), status_code=303)

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

    resp = RedirectResponse(_post_login_target(user["id"], welcome=True), status_code=303)
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

    resp = RedirectResponse(_post_login_target(bound_user["id"]), status_code=303)
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
    if tenant.needs_onboarding(uid):
        return RedirectResponse("/onboarding", status_code=303)

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
        "wechat_ready": bool(wx.get("appid") and wx.get("appsecret") and wx.get("verified")),
        "seed_ready": bool((u_row or {}).get("mcp_token")),
    })


# ---------- 分步引导配置与实时预览 Demo ----------

def _as_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val or "").strip().lower() in ("1", "true", "on", "yes")


@router.get("/preview-demo", response_class=HTMLResponse)
async def preview_demo(request: Request, theme: Optional[str] = None):
    """内置示例文案实时排版预览（用于引导向导与设置页右侧实时渲染）"""
    user = current_user(request)
    if not user:
        return HTMLResponse("<h1>401 未授权</h1>", status_code=401)
    uid = user["id"]
    cfg = tenant.get_tenant_config(uid)
    use_theme = (theme or cfg.get("theme") or "default").strip()
    valid_ids = {t["id"] for t in list_themes()}
    if use_theme not in valid_ids:
        use_theme = "default"
    author = ((cfg.get("wechat") or {}).get("author") or user.get("name") or "创作者").strip()
    raw_html = markdown_to_html(_DEMO_ARTICLE_MD)
    styled_html = apply_theme_for_preview(raw_html, use_theme)
    theme_label = _theme_name(use_theme)
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 18px 20px 28px;
  background: #ffffff;
  color: #1f2328;
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", Arial, sans-serif;
}}
.demo-head {{
  padding-bottom: 14px;
  margin-bottom: 16px;
  border-bottom: 1px solid #eef0f3;
}}
.demo-pill {{
  display: inline-block;
  font-size: 11px;
  font-weight: 600;
  color: #07c160;
  background: #eefaf3;
  border: 1px solid #bdebd0;
  padding: 2px 8px;
  border-radius: 999px;
  margin-bottom: 8px;
}}
.demo-meta {{
  font-size: 13px;
  color: #6b7280;
}}
</style>
</head>
<body>
  <div class="demo-head">
    <span class="demo-pill">当前预览主题：{theme_label}</span>
    <div class="demo-meta">公众号作者：{author} · 示例文案</div>
  </div>
  <div class="demo-body">{styled_html}</div>
</body>
</html>"""
    return HTMLResponse(html_doc)


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(
    request: Request,
    step: Optional[str] = None,
    welcome: Optional[str] = None,
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

    wx_verified = tenant.is_wechat_configured(uid)
    wx_secret_set = bool(wx.get("appsecret"))
    notion_token_set = bool(nt.get("token"))
    notion_wh_secret_set = bool(nt.get("webhook_secret"))
    notify_configured = bool(bark.get("key"))

    # 敏感凭据绝不回显
    if isinstance(wx, dict):
        wx["appsecret"] = ""
    if isinstance(nt, dict):
        nt["token"] = ""
        nt["webhook_secret"] = ""
    if isinstance(bark, dict):
        bark["key"] = ""

    # 若公众号作者为空，自动带入注册时填写的公众号名称
    if isinstance(wx, dict) and not (wx.get("author") or "").strip():
        wx["author"] = u_row.get("display_name") or ""

    req_step = _STEP_ALIASES.get((step or "").strip().lower(), "wechat")
    if not wx_verified:
        req_step = "wechat"

    platform_ai = config.load_config().get("ai_images") or {}
    platform_ai_ready = bool(
        platform_ai.get("base_url")
        and platform_ai.get("api_key")
        and platform_ai.get("text_model")
        and platform_ai.get("image_model")
    )

    return templates.TemplateResponse("onboarding.html", {
        "request": request,
        "user": user,
        "u": u_row,
        "cfg": cfg,
        "steps": _ONBOARDING_STEPS,
        "initial_step": req_step,
        "welcome": bool(welcome),
        "wx_verified": wx_verified,
        "wx_secret_set": wx_secret_set,
        "notion_token_set": notion_token_set,
        "notion_wh_secret_set": notion_wh_secret_set,
        "notify_configured": notify_configured,
        "themes": list_themes(),
        "current_theme": cfg.get("theme") or "default",
        "server_ip": os.getenv("SERVER_PUBLIC_IP", "161.153.84.30"),
        "platform_ai_ready": platform_ai_ready,
    })


@router.post("/onboarding/step/wechat")
async def onboarding_step_wechat(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    uid = user["id"]
    data, is_json = await _read_payload(request)
    old = tenant.get_tenant_config(uid)
    wx_old = old.get("wechat") or {}

    appid = str(data.get("appid") or data.get("wx_appid") or "").strip()
    raw_secret = str(data.get("appsecret") or data.get("wx_appsecret") or "").strip()
    appsecret = raw_secret or (wx_old.get("appsecret") or "")
    author = str(data.get("author") or data.get("wx_author") or "").strip()

    ok, msg = await _verify_wechat_credentials(appid, appsecret)
    if not ok:
        if is_json:
            return JSONResponse({"ok": False, "reason": msg}, status_code=400)
        return RedirectResponse(f"/onboarding?step=wechat", status_code=303)

    cfg = json.loads(json.dumps(old))
    cfg["wechat"] = {
        "appid": appid,
        "appsecret": appsecret,
        "author": author,
        "verified": True,
    }
    tenant.save_tenant_config(uid, cfg)
    logger.info("引导配置完成微信公众号验证与加密存储: user=%s", uid)

    if not is_json:
        return RedirectResponse("/onboarding?step=notion", status_code=303)
    return JSONResponse({
        "ok": True,
        "next_step": "notion",
        "encrypted_preview": "enc:v1:••••••••••••",
        "message": "公众号校验通过！AppSecret 已通过 AES-256-GCM 算法加密存入数据库。",
    })


@router.post("/onboarding/step/notion")
async def onboarding_step_notion(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    uid = user["id"]
    if tenant.needs_onboarding(uid):
        return JSONResponse({"ok": False, "reason": "请先完成第 1 步微信公众号验证"}, status_code=400)

    data, is_json = await _read_payload(request)
    if str(data.get("action") or "").strip().lower() == "skip":
        if not is_json:
            return RedirectResponse("/onboarding?step=ai", status_code=303)
        return JSONResponse({"ok": True, "skipped": True, "next_step": "ai"})

    old = tenant.get_tenant_config(uid)
    nt_old = old.get("notion") or {}
    raw_tok = str(data.get("token") or data.get("notion_token") or "").strip()
    raw_db = str(data.get("db_id") or data.get("notion_db_id") or "").strip()
    raw_wh = str(data.get("webhook_secret") or data.get("notion_webhook_secret") or "").strip()
    extracted_db = notion_db.extract_database_id(raw_db)

    cfg = json.loads(json.dumps(old))
    cfg["notion"] = {
        "token": raw_tok or (nt_old.get("token") or ""),
        "db_id": extracted_db,
        "webhook_secret": raw_wh or (nt_old.get("webhook_secret") or ""),
    }
    tenant.save_tenant_config(uid, cfg)
    notion_db.clear_cache(uid)
    if not is_json:
        return RedirectResponse("/onboarding?step=ai", status_code=303)
    return JSONResponse({
        "ok": True,
        "next_step": "ai",
        "db_id": extracted_db,
        "message": "Notion 数据源配置已保存",
    })


@router.post("/onboarding/step/ai")
async def onboarding_step_ai(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    uid = user["id"]
    if tenant.needs_onboarding(uid):
        return JSONResponse({"ok": False, "reason": "请先完成第 1 步微信公众号验证"}, status_code=400)

    data, is_json = await _read_payload(request)
    if str(data.get("action") or "").strip().lower() == "skip":
        if not is_json:
            return RedirectResponse("/onboarding?step=theme", status_code=303)
        return JSONResponse({"ok": True, "skipped": True, "next_step": "theme"})

    old = tenant.get_tenant_config(uid)
    ai_old = old.get("ai_images") or {}
    raw_min = str(data.get("skip_min_images") or data.get("ai_skip_min_images") or "").strip()
    try:
        skip_min = max(1, int(raw_min)) if raw_min else max(1, int(ai_old.get("skip_min_images") or 2))
    except (TypeError, ValueError):
        skip_min = 2

    cfg = json.loads(json.dumps(old))
    cfg["ai_images"] = {
        "enabled": _as_bool(data.get("enabled") if "enabled" in data else data.get("ai_enabled")),
        "style_mode": str(data.get("style_mode") or data.get("ai_style_mode") or "auto").strip() or "auto",
        "style": str(data.get("style") or data.get("ai_style") or "").strip(),
        "write_to_notion": _as_bool(data.get("write_to_notion") if "write_to_notion" in data else data.get("ai_write_notion")),
        "skip_if_has_images": _as_bool(data.get("skip_if_has_images") if "skip_if_has_images" in data else data.get("ai_skip_if_has_images")),
        "skip_min_images": skip_min,
    }
    tenant.save_tenant_config(uid, cfg)
    if not is_json:
        return RedirectResponse("/onboarding?step=theme", status_code=303)
    return JSONResponse({"ok": True, "next_step": "theme", "message": "AI 配图偏好已保存"})


@router.post("/onboarding/step/theme")
async def onboarding_step_theme(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    uid = user["id"]
    if tenant.needs_onboarding(uid):
        return JSONResponse({"ok": False, "reason": "请先完成第 1 步微信公众号验证"}, status_code=400)

    data, is_json = await _read_payload(request)
    if str(data.get("action") or "").strip().lower() == "skip":
        if not is_json:
            return RedirectResponse("/onboarding?step=notify", status_code=303)
        return JSONResponse({"ok": True, "skipped": True, "next_step": "notify"})

    theme_id = str(data.get("theme") or "default").strip() or "default"
    old = tenant.get_tenant_config(uid)
    cfg = json.loads(json.dumps(old))
    cfg["theme"] = theme_id
    tenant.save_tenant_config(uid, cfg)
    if not is_json:
        return RedirectResponse("/onboarding?step=notify", status_code=303)
    return JSONResponse({"ok": True, "next_step": "notify", "message": "排版主题已保存"})


@router.post("/onboarding/step/notify")
async def onboarding_step_notify(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    uid = user["id"]
    if tenant.needs_onboarding(uid):
        return JSONResponse({"ok": False, "reason": "请先完成第 1 步微信公众号验证"}, status_code=400)

    data, is_json = await _read_payload(request)
    if str(data.get("action") or "").strip().lower() == "skip":
        if not is_json:
            return RedirectResponse("/", status_code=303)
        return JSONResponse({"ok": True, "skipped": True, "done": True, "redirect": "/"})

    old = tenant.get_tenant_config(uid)
    bark_old = (old.get("notify") or {}).get("bark") or {}
    raw_key = str(data.get("key") or data.get("bark_key") or "").strip()

    cfg = json.loads(json.dumps(old))
    cfg["notify"] = {
        "enabled": _as_bool(data.get("enabled") if "enabled" in data else data.get("notify_enabled")),
        "bark": {
            "key": raw_key or (bark_old.get("key") or ""),
            "server": str(data.get("server") or data.get("bark_server") or "https://api.day.app").strip() or "https://api.day.app",
            "group": str(data.get("group") or data.get("bark_group") or "公众号发布").strip() or "公众号发布",
        },
        "events": {
            "start": _as_bool(data.get("ev_start")),
            "success": _as_bool(data.get("ev_success")),
            "error": _as_bool(data.get("ev_error")),
        },
    }
    tenant.save_tenant_config(uid, cfg)
    if not is_json:
        return RedirectResponse("/", status_code=303)
    return JSONResponse({"ok": True, "done": True, "redirect": "/", "message": "全部配置完成！"})


# ---------- 设置 ----------

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    saved: Optional[str] = None,
    welcome: Optional[str] = None,
    seed_reset: Optional[str] = None,
    seed_created: Optional[str] = None,
    pw_err: Optional[str] = None,
    wx_err: Optional[str] = None,
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
        "wx_ready": tenant.is_wechat_configured(uid),
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
        "wx_error": (wx_err or "").strip(),
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

    new_appid = (form.get("wx_appid") or "").strip()
    raw_wx_secret = (form.get("wx_appsecret") or "").strip()
    new_appsecret = raw_wx_secret or (wx_old.get("appsecret") or "")
    wx_verified = bool(wx_old.get("verified"))

    if new_appid or raw_wx_secret:
        changed = (new_appid != (wx_old.get("appid") or "").strip()) or bool(raw_wx_secret)
        if changed or not wx_verified:
            ok, msg = await _verify_wechat_credentials(new_appid, new_appsecret)
            if not ok:
                return RedirectResponse(f"/settings?wx_err={quote(msg)}", status_code=303)
        wx_verified = True
    elif "wx_appid" in form:
        new_appsecret = ""
        wx_verified = False
    else:
        new_appid = (wx_old.get("appid") or "").strip()
        new_appsecret = (wx_old.get("appsecret") or "")
        wx_verified = bool(wx_old.get("verified"))

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
            "appid": new_appid,
            "appsecret": new_appsecret,
            "author": (form.get("wx_author") or "").strip(),
            "verified": wx_verified,
        },
        "notion": {
            "token": (form.get("notion_token") or "").strip() or (nt_old.get("token") or ""),
            "db_id": notion_db.extract_database_id(form.get("notion_db_id") or ""),
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

    uid = user["id"]
    wx_old = (tenant.get_tenant_config(uid).get("wechat") or {})
    data, _ = await _read_payload(request)
    appid = str(data.get("appid") or data.get("wx_appid") or "").strip() or (wx_old.get("appid") or "").strip()
    appsecret = str(data.get("appsecret") or data.get("wx_appsecret") or "").strip() or (wx_old.get("appsecret") or "").strip()

    ok, msg = await _verify_wechat_credentials(appid, appsecret)
    if ok:
        return JSONResponse({"ok": True, "message": msg})
    return JSONResponse({"ok": False, "reason": msg})


@router.post("/settings/test-notion")
async def test_notion(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    uid = user["id"]
    nt_old = (tenant.get_tenant_config(uid).get("notion") or {})
    data, _ = await _read_payload(request)
    tok = str(data.get("token") or data.get("notion_token") or "").strip() or (nt_old.get("token") or "").strip()
    db_id = str(data.get("db_id") or data.get("notion_db_id") or "").strip() or (nt_old.get("db_id") or "").strip()
    if uid == tenant.OWNER_ID:
        if not tok:
            tok = os.getenv("NOTION_TOKEN", "").strip()
        if not db_id:
            db_id = (os.getenv("NOTION_DB_ID") or os.getenv("NOTION_DATABASE_ID") or "").strip()
    result = await notion_db.test_connection(tok, db_id)
    return JSONResponse(result)


@router.post("/settings/test-notify")
async def test_notify(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "reason": "unauthorized"}, status_code=401)

    data, _ = await _read_payload(request)
    bark_override = {
        "key": str(data.get("key") or data.get("bark_key") or "").strip(),
        "server": str(data.get("server") or data.get("bark_server") or "").strip(),
        "group": str(data.get("group") or data.get("bark_group") or "").strip(),
    }
    result = await notify.send(
        "发布台 · 测试通知",
        "如果你看到这条，说明 Bark 配好了。",
        user_id=user["id"],
        bark_override=bark_override,
    )
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
    if tenant.needs_onboarding(uid):
        return RedirectResponse("/onboarding", status_code=303)
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
    if tenant.needs_onboarding(uid):
        return RedirectResponse("/onboarding", status_code=303)
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
    if tenant.needs_onboarding(uid):
        return RedirectResponse("/onboarding", status_code=303)
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
