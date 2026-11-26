"""
文章存储（SQLite，支持多租户 user_id 隔离）
"""

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.getenv("DATA_DIR", "/app/data")) / "articles.db"


@dataclass
class Article:
    id: str
    title: str
    content: str  # Markdown
    html_content: str  # 渲染后的 HTML（内联样式，发布用）
    created_at: str
    updated_at: str
    published_at: Optional[str] = None
    draft_media_id: Optional[str] = None
    theme_id: str = "default"
    user_id: str = "owner"
    source: Optional[str] = None
    source_id: Optional[str] = None


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    """初始化数据库并兼容迁移列"""
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                html_content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                published_at TEXT,
                draft_media_id TEXT,
                theme_id TEXT DEFAULT 'default',
                source TEXT,
                source_id TEXT,
                user_id TEXT DEFAULT 'owner'
            )
        """)
        for stmt in (
            "ALTER TABLE articles ADD COLUMN theme_id TEXT DEFAULT 'default'",
            "ALTER TABLE articles ADD COLUMN source TEXT",
            "ALTER TABLE articles ADD COLUMN source_id TEXT",
            "ALTER TABLE articles ADD COLUMN user_id TEXT DEFAULT 'owner'",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_user ON articles(user_id)")
        conn.commit()
    finally:
        conn.close()


def init_source_columns():
    """兼容旧调用：确保 source / source_id / user_id 列存在"""
    init_db()


def _row_to_article(row: sqlite3.Row) -> Article:
    keys = row.keys()
    return Article(
        id=row["id"],
        title=row["title"],
        content=row["content"],
        html_content=row["html_content"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        published_at=row["published_at"],
        draft_media_id=row["draft_media_id"],
        theme_id=(row["theme_id"] if "theme_id" in keys and row["theme_id"] else "default"),
        user_id=(row["user_id"] if "user_id" in keys and row["user_id"] else "owner"),
        source=row["source"] if "source" in keys else None,
        source_id=row["source_id"] if "source_id" in keys else None,
    )


def create_article(
    title: str,
    content: str,
    html_content: str,
    theme_id: str = "default",
    user_id: str = "owner",
) -> Article:
    """创建文章"""
    init_db()
    article_id = uuid.uuid4().hex[:8]
    now = datetime.now().isoformat()
    uid = user_id or "owner"

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO articles (id, title, content, html_content, created_at, updated_at, theme_id, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (article_id, title, content, html_content, now, now, theme_id, uid),
        )
        conn.commit()
    finally:
        conn.close()

    return Article(
        id=article_id,
        title=title,
        content=content,
        html_content=html_content,
        created_at=now,
        updated_at=now,
        theme_id=theme_id,
        user_id=uid,
    )


def get_article(article_id: str, user_id: Optional[str] = None) -> Optional[Article]:
    """获取文章；传入 user_id 时强制校验归属"""
    init_db()
    conn = _connect()
    try:
        if user_id is not None:
            row = conn.execute(
                "SELECT * FROM articles WHERE id = ? AND COALESCE(user_id, 'owner') = ?",
                (article_id, user_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM articles WHERE id = ?",
                (article_id,),
            ).fetchone()
    finally:
        conn.close()

    return _row_to_article(row) if row else None


def update_article(
    article_id: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
    html_content: Optional[str] = None,
    theme_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[Article]:
    """更新文章"""
    article = get_article(article_id, user_id=user_id)
    if not article:
        return None

    new_title = title if title is not None else article.title
    new_content = content if content is not None else article.content
    new_html = html_content if html_content is not None else article.html_content
    new_theme = theme_id if theme_id is not None else article.theme_id
    now = datetime.now().isoformat()

    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE articles
            SET title = ?, content = ?, html_content = ?, updated_at = ?, theme_id = ?
            WHERE id = ?
            """,
            (new_title, new_content, new_html, now, new_theme, article_id),
        )
        conn.commit()
    finally:
        conn.close()

    return get_article(article_id, user_id=user_id)


def mark_published(
    article_id: str,
    draft_media_id: str,
    user_id: Optional[str] = None,
) -> Optional[Article]:
    """标记文章已发布"""
    now = datetime.now().isoformat()
    conn = _connect()
    try:
        if user_id is not None:
            conn.execute(
                """
                UPDATE articles
                SET published_at = ?, draft_media_id = ?
                WHERE id = ? AND COALESCE(user_id, 'owner') = ?
                """,
                (now, draft_media_id, article_id, user_id),
            )
        else:
            conn.execute(
                """
                UPDATE articles
                SET published_at = ?, draft_media_id = ?
                WHERE id = ?
                """,
                (now, draft_media_id, article_id),
            )
        conn.commit()
    finally:
        conn.close()

    return get_article(article_id, user_id=user_id)


def list_articles(user_id: Optional[str] = "owner") -> list[Article]:
    """列出文章；user_id=None 时列出全站所有文章"""
    init_db()
    conn = _connect()
    try:
        if user_id is None:
            rows = conn.execute("SELECT * FROM articles ORDER BY updated_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM articles WHERE COALESCE(user_id, 'owner') = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
    finally:
        conn.close()

    return [_row_to_article(row) for row in rows]


def upsert_from_source(
    title: str,
    content: str,
    html_content: str,
    theme_id: str,
    source: str,
    source_id: str,
    user_id: str = "owner",
) -> str:
    """按租户 + 来源 upsert 一篇文章，返回文章 id"""
    init_db()
    now = datetime.now().isoformat()
    uid = user_id or "owner"
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM articles WHERE COALESCE(user_id, 'owner') = ? AND source = ? AND source_id = ?",
            (uid, source, source_id),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE articles SET title=?, content=?, html_content=?, theme_id=?, updated_at=? WHERE id=?",
                (title, content, html_content, theme_id, now, row["id"]),
            )
            conn.commit()
            return row["id"]
        new_id = uuid.uuid4().hex[:8]
        conn.execute(
            "INSERT INTO articles (id, title, content, html_content, created_at, updated_at, theme_id, source, source_id, user_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new_id, title, content, html_content, now, now, theme_id, source, source_id, uid),
        )
        conn.commit()
        return new_id
    finally:
        conn.close()


def list_notion_source_map(user_id: str = "owner") -> dict[str, dict]:
    """返回某租户 {normalized_source_id: {id, updated_at, published_at}} 映射"""
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, source_id, updated_at, published_at FROM articles"
            " WHERE COALESCE(user_id, 'owner') = ? AND source = 'notion' AND source_id IS NOT NULL",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    mapping: dict[str, dict] = {}
    for r in rows:
        key = str(r["source_id"]).replace("-", "").lower()
        mapping[key] = {
            "id": r["id"],
            "updated_at": r["updated_at"] or "",
            "published_at": r["published_at"] or "",
        }
    return mapping
