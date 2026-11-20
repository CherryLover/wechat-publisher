"""
配图风格：不预设风格池，由模型按文章自由设计

之前有两版：
1) 风格写死成一段描述 → 每篇都一样
2) 预设 10 套让模型挑 → 选择有限，还是那几种

现在：风格完全交给模型，它读完文章自己定一套（媒介、配色、笔触、背景、光线）。
这个模块只负责三件事：
- 兜底：模型没给 / 给的东西不能用时，退回一套安全默认
- 判重：和最近几篇用过的一模一样就换掉
- 记账：把每篇用的风格记下来，喂给下一篇做「别重复」的参考
"""

import hashlib
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = lambda: Path(os.getenv("DATA_DIR", "/app/data")) / "articles.db"

NO_TEXT = "no text, no letters, no numbers, no logos, no watermarks"

# 兜底风格：模型没给出可用的描述时才用
DEFAULT_STYLE = (
    "modern flat vector editorial illustration on warm ivory background, "
    "muted teal as the primary color with a restrained warm apricot accent, "
    "clean dark outlines, soft diffuse light, subtle paper grain"
)
DEFAULT_NAME = "温润扁平（兜底）"

MIN_STYLE_LEN = 40          # 少于这个长度基本等于没写
MAX_STYLE_LEN = 600


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def slug(style_text: str) -> str:
    """给风格描述一个稳定短 id，用来判重"""
    return "auto-" + hashlib.sha1(normalize(style_text).encode("utf-8")).hexdigest()[:10]


def is_usable(style_text: str) -> bool:
    text = (style_text or "").strip()
    if len(text) < MIN_STYLE_LEN:
        return False
    # 至少要提到配色/媒介这类信息，避免模型糊一句 "beautiful picture"
    keywords = ("color", "palette", "background", "illustration", "style",
                "line", "light", "texture", "vector", "photo", "render", "paint")
    return any(k in text.lower() for k in keywords)


def clean_name(name: str, style_text: str) -> str:
    """风格名兜底：模型没给就取描述前几个词"""
    n = re.sub(r"\s+", "", (name or "").strip())[:24]
    if n:
        return n
    first = re.split(r"[,，.。]", (style_text or "").strip())[0]
    return (first[:16] or "自由风格")


# ---------- 历史记录 ----------

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
    # 老库补列（上一版只有 style_id）
    for col in ("style_name TEXT", "style_text TEXT"):
        try:
            conn.execute(f"ALTER TABLE ai_style_log ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    return conn


def log_use(article_ref: Optional[str], style_id: str, style_name: str,
            style_text: str, article_title: Optional[str] = None) -> None:
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO ai_style_log"
                " (created_at, article_ref, article_title, style_id, style_name, style_text)"
                " VALUES (?,?,?,?,?,?)",
                (datetime.now().isoformat(), article_ref, article_title,
                 style_id, (style_name or "")[:60], (style_text or "")[:MAX_STYLE_LEN]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def has_style_table() -> bool:
    try:
        conn = _conn()
        try:
            conn.execute("SELECT style_name FROM ai_style_log LIMIT 1")
            return True
        finally:
            conn.close()
    except Exception:
        return False


def _rows(sql: str, args: tuple) -> list:
    try:
        conn = _conn()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


def recent_styles(limit: int = 4) -> list:
    """最近用过的风格（按 style_id 去重，最近在前）"""
    rows = _rows(
        "SELECT style_id, style_name, style_text, article_title, created_at"
        " FROM ai_style_log ORDER BY id DESC LIMIT ?", (limit * 4,))
    out: list = []
    seen: set = set()
    for sid, name, text, title, at in rows:
        if sid in seen:
            continue
        seen.add(sid)
        out.append({"style_id": sid, "name": name or "", "text": text or "",
                    "title": title or "", "at": (at or "")[:16].replace("T", " ")})
        if len(out) >= limit:
            break
    return out


def recent_ids(limit: int = 4) -> list:
    return [s["style_id"] for s in recent_styles(limit)]


def repeated(style_text: str, limit: int = 4) -> bool:
    """和最近几篇的风格描述是否完全一致"""
    sig = normalize(style_text)
    if not sig:
        return False
    return any(normalize(s["text"]) == sig for s in recent_styles(limit))


def recent_log(limit: int = 20) -> list:
    rows = _rows(
        "SELECT created_at, style_id, style_name, article_ref, article_title"
        " FROM ai_style_log ORDER BY id DESC LIMIT ?", (limit,))
    return [
        {"at": (r[0] or "")[:16].replace("T", " "), "style_id": r[1],
         "name": r[2] or r[1], "ref": (r[3] or "")[:8], "title": (r[4] or "")[:28]}
        for r in rows
    ]
