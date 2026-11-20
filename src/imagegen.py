"""
AI 配图：文章 → 4 张图（1 封面 + 3 插图）

流程：
① 文生文模型读文章，产出 4 条绘图提示词 + 每张插图的插入锚点
② 生图模型逐张出图，压成微信可用的 JPEG（<900KB）
③ 3 张插图按锚点插回 Markdown，封面单独返回（给草稿 thumb 用）

生图通道兼容两种网关：
- images：POST /images/generations（标准 OpenAI）
- responses：POST /responses + tools=[{"type":"image_generation"}]（有些中转站只开了这条）
- auto：先试 images，失败退回 responses

每次调用都用 usage.record() 记 token/耗时，供设置页的用量面板统计。
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from . import config, imagestyles, usage
from .imagestyles import NO_TEXT

logger = logging.getLogger(__name__)

# 提示词里限制死的画风约束（可在设置页覆盖风格描述）
BASE_STYLE = (
    "modern flat vector editorial illustration, warm ivory background, "
    "one muted primary color plus one restrained warm accent, clean dark outlines, "
    "soft diffuse light, subtle paper grain, generous negative space, "
    "no text, no letters, no numbers, no logos, no watermarks"
)

PLAN_SYSTEM = """你是一位资深公众号视觉编辑，擅长给技术类文章配图。

任务：读一篇文章，为它设计 4 张配图 —— 1 张封面 + 3 张正文插图。

硬性要求：
1. 4 张图必须共享同一套视觉语言（见下方「指定风格」），彼此场景不重复。
2. 画面里绝对不要出现任何文字、字母、数字、logo、水印（生图模型画文字必糊）。
3. 封面图：概括全文主题，主体居中偏侧，左侧/上方留白（公众号头图会被裁成 2.35:1）。
4. 3 张内容插图：分别对应文章里 3 个最值得配图的位置。
5. 每张插图必须给出插入锚点：
   - heading：必须是正文中真实存在的某个 ## 二级标题原文（不要带 ## 和首尾空格）
   - after_paragraph：插在这个标题下面第几个自然段之后（1 表示第一段之后，通常 1 最自然）
6. prompt 一律用英文写，要具体画得出来：主体、动作、构图、视角、光线、配色。

6. 风格不是固定的：从给定的「风格候选」里挑一套最贴合这篇文章气质的，
   整篇 4 张图都用这一套；如果给了「最近用过」的清单，尽量别再选它们。
7. 把选中的风格 id 填到 style_id；每张图的 prompt 都要体现这套风格的配色与笔触。

只输出 JSON：
{
  "style_id": "从风格候选里选的 id（若给的是固定风格，填 custom）",
  "style": "这套图的统一风格一句话（英文）",
  "cover": {"subject": "对应文章哪个主题（中文）", "prompt": "英文提示词", "alt": "中文图注 10-20 字"},
  "illustrations": [
    {"heading": "## 标题原文（不含 ##）", "after_paragraph": 1, "why": "为什么配在这里（中文）", "prompt": "英文提示词", "alt": "中文图注 10-20 字"},
    {"heading": "...", "after_paragraph": 1, "why": "...", "prompt": "...", "alt": "..."},
    {"heading": "...", "after_paragraph": 1, "why": "...", "prompt": "...", "alt": "..."}
  ]
}"""

MAX_BODY_CHARS = 6000

# auto 模式下，一旦发现 images/generations 不可用，本次进程内就不再重试
_IMAGES_ENDPOINT_BROKEN = False

# 聚合网关常见 5xx / 524（Cloudflare 上游超时），值得重试一次
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
IMAGE_ATTEMPTS = 2
RETRY_DELAY = 3.0


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code in _RETRYABLE_STATUS or code >= 500
    return False


class AIError(Exception):
    pass


@dataclass
class AIResult:
    markdown: str
    plan: dict = field(default_factory=dict)
    cover_bytes: Optional[bytes] = None
    cover_url: Optional[str] = None
    style_id: str = ""
    style_name: str = ""
    items: list = field(default_factory=list)   # 成功的插图：[{kind,url,alt,heading,path}]

    @property
    def ok(self) -> bool:
        return bool(self.items) or bool(self.cover_bytes)


# ---------- 配置 ----------

def get_cfg() -> dict:
    return (config.load_config().get("ai_images") or {})


def is_enabled(cfg: Optional[dict] = None) -> bool:
    c = cfg if cfg is not None else get_cfg()
    return bool(c.get("enabled")) and bool((c.get("api_key") or "").strip())


def public_base() -> str:
    import os
    return (os.getenv("BASE_URL") or "https://publisher.flyooo.uk").rstrip("/")


# ---------- HTTP ----------

def _headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg.get('api_key')}", "Content-Type": "application/json"}


def _url(cfg: dict, path: str) -> str:
    return f"{(cfg.get('base_url') or '').rstrip('/')}/{path.lstrip('/')}"


def _num(d: dict, *keys) -> int:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _tokens_from(usage_obj: Optional[dict]) -> tuple[int, int, int]:
    """兼容 chat/responses 两种 usage 结构"""
    if not usage_obj:
        return 0, 0, 0
    p = _num(usage_obj, "prompt_tokens", "input_tokens")
    c = _num(usage_obj, "completion_tokens", "output_tokens")
    t = _num(usage_obj, "total_tokens") or (p + c)
    return p, c, t


def apply_style(prompt: str, style_en: str) -> str:
    """把本篇风格钉在提示词末尾；已经包含就不重复追加"""
    p = (prompt or "").strip()
    if style_en and style_en.lower() not in p.lower():
        p += f"\n\nVisual style: {style_en}. {NO_TEXT}."
    return p


def resolve_style(cand: str, cfg: dict, ref: str, pool: list,
                  fixed_style: Optional[dict] = None) -> tuple[dict, bool]:
    """决定本篇用哪套风格。返回 (风格, 是否走了兜底)"""
    if fixed_style is not None:
        return fixed_style, False

    pool_ids = [x["id"] for x in (pool or [])]
    recent = imagestyles.recent_ids(4)
    chosen = imagestyles.get(cand) if cand in pool_ids else None
    if chosen is None:
        return imagestyles.pick(ref, allow=cfg.get("style_allow"), recent=recent), True
    # 模型选了最近刚用过的风格，且池子里还有别的选择 → 换一个
    if cand in recent and len(pool_ids) > len(recent):
        return imagestyles.pick(ref, allow=cfg.get("style_allow"), recent=recent), True
    return chosen, False



def _style_brief(cfg: dict):
    """根据设置生成「风格说明」段落，并给出兜底风格"""
    mode = (cfg.get("style_mode") or "auto").lower()
    allow = cfg.get("style_allow") or []

    if mode == "fixed":
        text = (cfg.get("style") or "").strip() or BASE_STYLE
        brief = f"固定风格（4 张图都必须严格照这套画）：{text}"
        return brief, {"id": "custom", "name": "自定义", "desc_en": text}, []

    pool = imagestyles.allowed(allow)
    recent = imagestyles.recent_ids(4)
    catalog = imagestyles.prompt_catalog(pool)
    recent_txt = ""
    if recent:
        names = [f"{i}（{(imagestyles.get(i) or {}).get('name', i)}）" for i in recent]
        recent_txt = "\n最近几篇已经用过这些风格，请尽量避开：" + "、".join(names)
    brief = (f"风格候选（style_id 必须从中选一个）：\n{catalog}{recent_txt}\n"
             f"候选之外的风格不要选；画面里不要出现文字。")
    return brief, None, pool



async def plan_prompts(cfg: dict, title: str, md: str,
                       article_ref: Optional[str] = None,
                       article_title: Optional[str] = None) -> dict:
    """第①步：文章 → 4 条提示词"""
    body = md.strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n\n（正文过长，已截断）"

    style_brief, fixed_style, pool = _style_brief(cfg)
    user_msg = (f"{style_brief}\n\n基础画风约束：{BASE_STYLE}\n\n"
                f"文章标题：{title}\n\n正文：\n{body}\n\n请按约定输出 JSON。")

    payload = {
        "model": cfg.get("text_model"),
        "messages": [
            {"role": "system", "content": PLAN_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.7,
    }

    t0 = time.time()
    err = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=20.0),
                                     follow_redirects=True) as cli:
            r = await cli.post(_url(cfg, "chat/completions"), json=payload, headers=_headers(cfg))
            if r.status_code == 400 and "response_format" in r.text:
                payload["response_format"] = {"type": "json_object"}
                r = await cli.post(_url(cfg, "chat/completions"), json=payload, headers=_headers(cfg))
            r.raise_for_status()
            data = r.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
        clean = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        clean = re.sub(r"\n?```$", "", clean).strip()
        plan = json.loads(clean)
        if not plan.get("cover") or not plan.get("illustrations"):
            raise AIError(f"模型返回结构不对：{str(plan)[:200]}")

        # 风格校验：固定模式直接用；自动模式校验候选 + 避开最近用过
        cand = (plan.get("style_id") or "").strip()
        chosen, fallback = resolve_style(cand, cfg, article_ref or title, pool, fixed_style)
        if fallback and fixed_style is None:
            logger.info("风格未采用模型选择（%r），兜底为 %s", cand, chosen["id"])
        plan["_style"] = chosen
        plan["_style_en"] = chosen.get("desc_en") or BASE_STYLE
        p, c, t = _tokens_from(data.get("usage"))
        usage.record(step="plan", model=cfg.get("text_model"), prompt_tokens=p,
                     completion_tokens=c, total_tokens=t,
                     duration_ms=int((time.time() - t0) * 1000),
                     article_ref=article_ref, article_title=article_title)
        return plan
    except Exception as e:
        err = e
        usage.record(step="plan", model=cfg.get("text_model"), ok=False, error=str(e),
                     duration_ms=int((time.time() - t0) * 1000),
                     article_ref=article_ref, article_title=article_title)
        raise AIError(f"生成配图提示词失败：{e}") from e


async def generate_image(cfg: dict, prompt: str, size: str, *,
                         step: str, article_ref: Optional[str] = None,
                         article_title: Optional[str] = None) -> bytes:
    """第②步：一条提示词 → 一张图（原始字节）。5xx/超时自动重试一次。"""
    api = (cfg.get("image_api") or "auto").lower()
    model = cfg.get("image_model")
    t0 = time.time()
    last_err: Optional[Exception] = None

    for attempt in range(1, IMAGE_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=20.0),
                                         follow_redirects=True) as cli:
                raw, u = await _dispatch(cli, cfg, model, prompt, size, api)
            p, c, t = _tokens_from(u)
            usage.record(step=step, model=model, prompt_tokens=p, completion_tokens=c,
                         total_tokens=t, images=1,
                         duration_ms=int((time.time() - t0) * 1000),
                         article_ref=article_ref, article_title=article_title)
            return raw
        except Exception as e:
            last_err = e
            first_line = str(e).splitlines()[0] if str(e) else type(e).__name__
            if attempt < IMAGE_ATTEMPTS and _retryable(e):
                logger.warning("%s 第 %d 次失败（%s），%.0fs 后重试",
                               step, attempt, first_line[:100], RETRY_DELAY)
                await asyncio.sleep(RETRY_DELAY)
                continue
            usage.record(step=step, model=model, ok=False, error=str(e),
                         duration_ms=int((time.time() - t0) * 1000),
                         article_ref=article_ref, article_title=article_title)
            raise

    raise last_err or AIError("生图失败")


async def _dispatch(cli, cfg, model, prompt, size, api) -> tuple[bytes, dict]:
    if api == "responses":
        return await _via_responses(cli, cfg, model, prompt, size)
    if api == "images":
        return await _via_images(cli, cfg, model, prompt, size)

    global _IMAGES_ENDPOINT_BROKEN
    if _IMAGES_ENDPOINT_BROKEN:
        return await _via_responses(cli, cfg, model, prompt, size)
    try:
        return await _via_images(cli, cfg, model, prompt, size)
    except Exception as e1:
        _IMAGES_ENDPOINT_BROKEN = True
        first_line = str(e1).splitlines()[0] if str(e1) else ""
        logger.warning("images 接口不可用，本次进程改用 responses 工具：%s", first_line[:100])
        return await _via_responses(cli, cfg, model, prompt, size)


async def _via_images(cli, cfg, model, prompt, size) -> tuple[bytes, dict]:
    payload = {"model": model, "prompt": prompt, "n": 1, "size": size,
               "response_format": "b64_json"}
    r = await cli.post(_url(cfg, "images/generations"), json=payload, headers=_headers(cfg))
    if r.status_code == 400 and "response_format" in r.text:
        payload.pop("response_format", None)
        r = await cli.post(_url(cfg, "images/generations"), json=payload, headers=_headers(cfg))
    r.raise_for_status()
    d = r.json()
    item = (d.get("data") or [{}])[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), d.get("usage") or {}
    if item.get("url"):
        img = await cli.get(item["url"])
        img.raise_for_status()
        return img.content, d.get("usage") or {}
    raise AIError(f"生图响应里没有图片：{str(d)[:200]}")


async def _via_responses(cli, cfg, model, prompt, size) -> tuple[bytes, dict]:
    tool = {"type": "image_generation", "size": size}
    r = await cli.post(_url(cfg, "responses"),
                       json={"model": model, "input": prompt, "tools": [tool]},
                       headers=_headers(cfg))
    r.raise_for_status()
    d = r.json()
    for it in d.get("output") or []:
        if it.get("type") == "image_generation_call" and it.get("result"):
            return base64.b64decode(it["result"]), d.get("usage") or {}
    raise AIError(f"Responses 里没有 image_generation_call：{str(d)[:250]}")


# ---------- 图片后处理 ----------

def normalize_jpeg(raw: bytes, out_path: Path, max_width: int = 1200,
                   quality: int = 88, limit: int = 900 * 1024) -> tuple[int, int, int]:
    """转成微信友好的 JPEG 并压到 limit 以下。返回 (宽, 高, 字节数)"""
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    if max_width and img.width > int(max_width):
        h = int(img.height * int(max_width) / img.width)
        img = img.resize((int(max_width), h), Image.LANCZOS)

    data = b""
    for q in (quality, 82, 76, 70, 62, 55):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
        data = buf.getvalue()
        if len(data) <= limit:
            break
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return img.width, img.height, len(data)


# ---------- 插入 Markdown ----------

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).strip().lower()


def insert_images(md: str, items: list) -> str:
    """按模型给的锚点把插图插进 Markdown。

    items: [{heading, after_paragraph, url, alt}]
    """
    lines = md.split("\n")

    def is_heading(line: str) -> bool:
        return bool(re.match(r"^#{1,6}\s", line))

    heads = [(i, lines[i]) for i in range(len(lines)) if is_heading(lines[i])]
    inserts: list[tuple[int, str]] = []   # (插到第几行之后, 图片 markdown)

    for it in items:
        target = _norm(it.get("heading"))
        anchor = None
        # 先找标题完全一致的，再退化成包含匹配
        for idx, line in heads:
            text = _norm(re.sub(r"^#{1,6}\s*", "", line))
            if text == target:
                anchor = idx
                break
        if anchor is None:
            for idx, line in heads:
                text = _norm(re.sub(r"^#{1,6}\s*", "", line))
                if target and (target in text or text in target):
                    anchor = idx
                    break
        if anchor is None:
            # 模型给的锚点对不上（少见）→ 宁可放文末，也别丢掉这张已经花钱生成的图
            logger.warning("锚点没匹配上：%r，改插到文末", it.get("heading"))
            inserts.append((len(lines) - 1, f"![{(it.get('alt') or '插图')}]({it['url']})"))
            continue

        # 该小节的范围
        end = len(lines)
        for idx, _ in heads:
            if idx > anchor:
                end = idx
                break

        # 数小节里的自然段（连续非空行算一段）
        want = int(it.get("after_paragraph") or 1)
        para_end, count, i = None, 0, anchor + 1
        while i < end:
            if lines[i].strip():
                j = i
                while j + 1 < end and lines[j + 1].strip():
                    j += 1
                count += 1
                if count >= want:
                    para_end = j
                    break
                i = j + 1
            else:
                i += 1
        pos = para_end if para_end is not None else (end - 1)
        alt = (it.get("alt") or "插图").replace("]", ")")
        inserts.append((pos, f"![{alt}]({it['url']})"))

    # 从后往前插，位置才不会错位
    for pos, img in sorted(inserts, key=lambda x: x[0], reverse=True):
        lines.insert(pos + 1, img)
        lines.insert(pos + 2, "")

    return "\n".join(lines)


# ---------- 主流程 ----------

async def build(title: str, markdown: str, *, ref: str, upload_dir: Path,
                base: Optional[str] = None) -> AIResult:
    """给一篇文章生成 4 张图，返回插好图的 Markdown + 封面字节。"""
    cfg = get_cfg()
    if not is_enabled(cfg):
        return AIResult(markdown=markdown)

    base = (base or public_base()).rstrip("/")
    upload_dir = Path(upload_dir)
    tag = hashlib.sha1((ref or title).encode("utf-8")).hexdigest()[:8]

    plan = await plan_prompts(cfg, title, markdown,
                              article_ref=ref, article_title=title)

    # plan_prompts 正常会把 _style 放进来；被替换/老计划时这里补一次兜底
    style = plan.get("_style")
    if not style:
        _, fixed_style, pool = _style_brief(cfg)
        style, _ = resolve_style((plan.get("style_id") or "").strip(),
                                 cfg, ref, pool, fixed_style)
        plan["_style"] = style
        plan["_style_en"] = style.get("desc_en") or BASE_STYLE
    style_en = plan.get("_style_en") or BASE_STYLE
    try:
        imagestyles.log_use(ref, style.get("id") or "custom", title)
    except Exception:
        pass
    logger.info("本篇配图风格：%s（%s）", style.get("name") or "自定义", style.get("id") or "custom")

    def with_style(prompt: str) -> str:
        return apply_style(prompt, style_en)

    cover_size = cfg.get("cover_size") or "1536x1024"
    ill_size = cfg.get("illustration_size") or "1536x1024"
    max_width = int(cfg.get("max_width") or 1200)

    result = AIResult(markdown=markdown, plan=plan)

    # 封面：本地已有同名文件就复用（稳定 URL，重复发布不重复烧钱）
    cover_path = upload_dir / f"ai-{tag}-cover.jpg"
    try:
        if cover_path.exists():
            logger.info("复用已有 AI 封面 %s", cover_path.name)
        else:
            raw = await generate_image(cfg, with_style(plan["cover"]["prompt"]), cover_size,
                                       step="cover", article_ref=ref, article_title=title)
            normalize_jpeg(raw, cover_path, max_width=max_width)
        result.cover_bytes = cover_path.read_bytes()
        result.cover_url = f"{base}/images/{cover_path.name}"
        result.style_id = style.get("id") or "custom"
        result.style_name = style.get("name") or "自定义"
    except Exception as e:
        logger.warning("AI 封面生成失败：%s", e)

    # 3 张插图
    items, new_items = [], []
    for i, ill in enumerate((plan.get("illustrations") or [])[:3], start=1):
        if not ill.get("prompt"):
            continue
        name = f"ai-{tag}-ill-{i}.jpg"
        url = f"{base}/images/{name}"
        path = upload_dir / name

        # 正文里已经有这张图（上次发布回写进 Notion 了）→ 跳过，避免重复插
        if url in markdown:
            logger.info("正文里已有 %s，跳过生成", name)
            items.append({"kind": f"illustration-{i}", "heading": ill.get("heading") or "",
                          "after_paragraph": ill.get("after_paragraph") or 1,
                          "alt": ill.get("alt") or f"插图 {i}", "url": url,
                          "path": str(path), "reused": True})
            continue

        try:
            if path.exists():
                logger.info("复用已有 AI 插图 %s", name)
            else:
                raw = await generate_image(cfg, with_style(ill["prompt"]), ill_size,
                                           step=f"illustration-{i}",
                                           article_ref=ref, article_title=title)
                normalize_jpeg(raw, path, max_width=max_width)
            item = {
                "kind": f"illustration-{i}",
                "heading": ill.get("heading") or "",
                "after_paragraph": ill.get("after_paragraph") or 1,
                "alt": ill.get("alt") or f"插图 {i}",
                "url": url,
                "path": str(path),
            }
            items.append(item)
            new_items.append(item)
        except Exception as e:
            logger.warning("AI 插图 %s 生成失败：%s", i, e)

    result.items = items
    if new_items:
        result.markdown = insert_images(markdown, new_items)
    return result


async def test_connection(cfg: Optional[dict] = None) -> dict:
    """设置页的「测试连接」：只拉一次模型列表，不烧生图额度"""
    c = cfg or get_cfg()
    if not (c.get("base_url") and c.get("api_key")):
        return {"ok": False, "reason": "base URL 或 API Key 没填"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cli:
            r = await cli.get(_url(c, "models"), headers=_headers(c))
        if r.status_code != 200:
            return {"ok": False, "reason": f"HTTP {r.status_code}: {r.text[:120]}"}
        ids = [m.get("id") for m in (r.json().get("data") or [])]
        text_ok = (c.get("text_model") in ids) if c.get("text_model") else None
        image_ok = (c.get("image_model") in ids) if c.get("image_model") else None
        return {"ok": True, "models": len(ids), "text_model_found": text_ok,
                "image_model_found": image_ok, "sample": ids[:8]}
    except Exception as e:
        return {"ok": False, "reason": str(e)}
