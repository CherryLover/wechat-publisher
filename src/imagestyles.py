"""
配图风格池

问题：风格写死成一个字符串，每篇文章配图都长一个样。
做法：维护一组预设风格，让文生文模型读完文章后自己挑一套最贴合的，
      同时把最近用过的风格告诉它、"尽量不要重复"，代码再做一层兜底轮换。

风格描述用英文（直接喂给生图模型），name/mood 用中文（给设置页看）。
"""

import hashlib
import os
import sqlite3
from datetime import datetime
from typing import Optional

DB_PATH = lambda: __import__("pathlib").Path(os.getenv("DATA_DIR", "/app/data")) / "articles.db"

# 每套风格都自带「画面里不要出现文字」的约束，避免生图糊字
NO_TEXT = "no text, no letters, no numbers, no logos, no watermarks"

STYLES = [
    {
        "id": "warm_flat",
        "name": "温润扁平",
        "mood": "通用、温和",
        "colors": ["#faf7f1", "#2f7d78", "#e29567"],
        "desc_en": ("modern flat vector editorial illustration on warm ivory background, "
                    "muted teal as the primary color with a restrained warm apricot accent, "
                    "clean dark outlines, soft diffuse light, subtle paper grain"),
    },
    {
        "id": "tech_dark",
        "name": "深色科技",
        "mood": "模型、工具、硬核技术",
        "colors": ["#0d1b2a", "#22d3ee", "#a78bfa"],
        "desc_en": ("dark techno editorial illustration on a deep navy background, "
                    "neon cyan and soft violet accents, thin glowing geometric lines, "
                    "faint grid, high contrast light, futuristic and clean"),
    },
    {
        "id": "magazine",
        "name": "杂志拼贴",
        "mood": "观点、评论、行业观察",
        "colors": ["#f2ece0", "#1f1f1f", "#b5423a"],
        "desc_en": ("editorial magazine collage on warm beige paper, bold black brush strokes, "
                    "brick red spot color, cut-paper shapes, subtle halftone dots, "
                    "loose handmade composition"),
    },
    {
        "id": "blueprint",
        "name": "蓝图线稿",
        "mood": "教程、架构、原理拆解",
        "colors": ["#12365c", "#e8f1ff", "#7fb2e5"],
        "desc_en": ("architectural blueprint illustration, deep blue background, "
                    "fine white line work, technical cross-sections and isometric construction, "
                    "precise measuring marks"),
    },
    {
        "id": "retro_print",
        "name": "复古印刷",
        "mood": "人文、历史、产品回顾",
        "colors": ["#d9c9a3", "#1e1b16", "#c8452d"],
        "desc_en": ("retro risograph print, ochre paper, two-color ink of black and vermilion, "
                    "visible slight misregistration, coarse grain, mid-century poster feel"),
    },
    {
        "id": "minimal_line",
        "name": "极简线描",
        "mood": "深度长文、思辨",
        "colors": ["#ffffff", "#2b2b2b", "#9aa7b1"],
        "desc_en": ("minimal single-color line illustration on pure white, "
                    "ultra-thin uniform line work, generous negative space, "
                    "one soft grey-blue accent, quiet and restrained"),
    },
    {
        "id": "bright_flat",
        "name": "明亮扁平",
        "mood": "科普、入门、轻快",
        "colors": ["#ffffff", "#ffd23f", "#2b6cb0"],
        "desc_en": ("bright flat vector illustration on pure white, "
                    "saturated yellow and blue geometric shapes, bold playful forms, "
                    "crisp edges, cheerful and energetic"),
    },
    {
        "id": "iso3d",
        "name": "等距 3D",
        "mood": "产品、工具、工作流",
        "colors": ["#eaeef3", "#5b7c99", "#e8a87c"],
        "desc_en": ("soft 3D isometric illustration, pale grey-blue background, "
                    "rounded low-poly objects, gentle studio lighting, "
                    "subtle contact shadows, tidy miniature scene"),
    },
    {
        "id": "watercolor",
        "name": "水彩",
        "mood": "随笔、生活、情感",
        "colors": ["#fbf7f0", "#8fb3c9", "#d9a5a5"],
        "desc_en": ("delicate watercolor illustration on textured paper, translucent washes, "
                    "soft muted palette, gentle bleeding edges, airy and calm"),
    },
    {
        "id": "paper_cut",
        "name": "剪纸层叠",
        "mood": "生活方式、轻松话题",
        "colors": ["#f7f3ea", "#3d7068", "#e9b872"],
        "desc_en": ("layered paper-cut illustration with tactile paper shadows, "
                    "multiple flat color layers, warm cheerful palette, "
                    "clean depth and simple bold silhouettes"),
    },
]

_BY_ID = {s["id"]: s for s in STYLES}
ALL_IDS = [s["id"] for s in STYLES]


def get(style_id: str) -> Optional[dict]:
    return _BY_ID.get(style_id)


def allowed(allow: Optional[list]) -> list:
    """按设置里的白名单过滤；空/全不选时回退到全部"""
    ids = [i for i in (allow or []) if i in _BY_ID]
    return [s for s in STYLES if s["id"] in ids] if ids else list(STYLES)


def prompt_catalog(pool: list) -> str:
    """给文生文模型看的候选清单"""
    lines = []
    for s in pool:
        lines.append(f"- {s['id']}（{s['name']}，适合：{s['mood']}）：{s['desc_en']}")
    return "\n".join(lines)


def pick(ref: str, allow: Optional[list] = None, recent: Optional[list] = None) -> dict:
    """兜底选择：按文章 ref 稳定散列，且避开最近用过的风格"""
    pool = allowed(allow)
    fresh = [s for s in pool if s["id"] not in (recent or [])] or pool
    idx = int(hashlib.sha1((ref or "x").encode("utf-8")).hexdigest(), 16) % len(fresh)
    return fresh[idx]


# ---------- 使用记录（用来避免连续重复） ----------

def _conn() -> sqlite3.Connection:
    path = DB_PATH()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_style_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            article_ref TEXT,
            article_title TEXT,
            style_id TEXT NOT NULL
        )
    """)
    return conn


def log_use(article_ref: Optional[str], style_id: str, article_title: Optional[str] = None) -> None:
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO ai_style_log (created_at, article_ref, article_title, style_id)"
                " VALUES (?,?,?,?)",
                (datetime.now().isoformat(), article_ref, article_title, style_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def recent_ids(limit: int = 4) -> list:
    """最近用过的风格 id（去重后保持最近顺序）"""
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT style_id FROM ai_style_log ORDER BY id DESC LIMIT ?", (limit * 3,)
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    out: list = []
    for (sid,) in rows:
        if sid not in out:
            out.append(sid)
        if len(out) >= limit:
            break
    return out


def recent_log(limit: int = 20) -> list:
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT created_at, style_id, article_ref, article_title"
                " FROM ai_style_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [
        {"at": (r[0] or "")[:16].replace("T", " "), "style_id": r[1],
         "ref": (r[2] or "")[:8], "title": (r[3] or "")[:28],
         "name": (_BY_ID.get(r[1]) or {}).get("name", r[1])}
        for r in rows
    ]
