"""
API 用量统计（SQLite，和文章库同一个库文件）

每次调用大模型/生图接口都落一条记录：
- step：plan（文生文规划）/ cover（封面）/ illustration-1..3
- model、prompt/completion/total tokens、耗时、成败、错误信息
- article_ref：本地文章 id 或 Notion page id，用来做「单篇用量」聚合
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.getenv("DATA_DIR", "/app/data")) / "articles.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            article_ref TEXT,
            article_title TEXT,
            step TEXT NOT NULL,
            model TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            images INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            ok INTEGER DEFAULT 1,
            error TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_ref ON api_usage(article_ref)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_time ON api_usage(created_at)")
    return conn


def record(
    *,
    step: str,
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    images: int = 0,
    duration_ms: int = 0,
    ok: bool = True,
    error: Optional[str] = None,
    article_ref: Optional[str] = None,
    article_title: Optional[str] = None,
) -> None:
    """记一条调用。失败也不能影响主流程。"""
    try:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO api_usage
                   (created_at, article_ref, article_title, step, model,
                    prompt_tokens, completion_tokens, total_tokens, images,
                    duration_ms, ok, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now().isoformat(), article_ref, article_title,
                    step, model, int(prompt_tokens or 0), int(completion_tokens or 0),
                    int(total_tokens or 0), int(images or 0), int(duration_ms or 0),
                    1 if ok else 0, (error or "")[:400] or None,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def totals() -> dict:
    """全局汇总"""
    conn = _connect()
    try:
        row = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(total_tokens),0),
                   COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0),
                   COALESCE(SUM(images),0), COALESCE(SUM(ok=0),0)
            FROM api_usage
        """).fetchone()
    finally:
        conn.close()
    return {
        "calls": row[0] or 0,
        "total_tokens": row[1] or 0,
        "prompt_tokens": row[2] or 0,
        "completion_tokens": row[3] or 0,
        "images": row[4] or 0,
        "errors": row[5] or 0,
    }


def by_step() -> list[dict]:
    """按步骤汇总（plan / cover / illustration）"""
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT step, COUNT(*), COALESCE(SUM(total_tokens),0), COALESCE(SUM(images),0)
            FROM api_usage GROUP BY step ORDER BY step
        """).fetchall()
    finally:
        conn.close()
    return [{"step": r[0], "calls": r[1], "tokens": r[2], "images": r[3]} for r in rows]


def by_article(limit: int = 30) -> list[dict]:
    """单篇汇总：同一篇文章的所有调用加起来"""
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT article_ref,
                   MAX(article_title),
                   COUNT(*),
                   COALESCE(SUM(total_tokens),0),
                   COALESCE(SUM(images),0),
                   MIN(created_at), MAX(created_at)
            FROM api_usage
            WHERE article_ref IS NOT NULL AND article_ref != ''
            GROUP BY article_ref
            ORDER BY MAX(created_at) DESC
            LIMIT ?
        """, (limit,)).fetchall()
    finally:
        conn.close()
    return [
        {
            "ref": r[0], "title": r[1] or "(未知)", "calls": r[2],
            "tokens": r[3], "images": r[4],
            "first_at": (r[5] or "")[:16].replace("T", " "),
            "last_at": (r[6] or "")[:16].replace("T", " "),
        }
        for r in rows
    ]


def recent(limit: int = 50) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT created_at, step, model, total_tokens, images, duration_ms, ok, error,
                   article_ref, article_title
            FROM api_usage ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
    finally:
        conn.close()
    return [
        {
            "at": (r[0] or "")[:19].replace("T", " "),
            "step": r[1], "model": r[2] or "",
            "tokens": r[3] or 0, "images": r[4] or 0,
            "ms": r[5] or 0, "ok": bool(r[6]), "error": r[7] or "",
            "ref": (r[8] or "")[:8], "title": (r[9] or "")[:28],
        }
        for r in rows
    ]
