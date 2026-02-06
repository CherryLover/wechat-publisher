"""
文章存储（SQLite）
"""

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

DB_PATH = Path("/app/data/articles.db")


@dataclass
class Article:
    id: str
    title: str
    content: str  # Markdown
    html_content: str  # 渲染后的 HTML
    created_at: str
    updated_at: str
    published_at: Optional[str] = None
    draft_media_id: Optional[str] = None


def init_db():
    """初始化数据库"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            html_content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT,
            draft_media_id TEXT
        )
    """)
    conn.commit()
    conn.close()


def _row_to_article(row: tuple) -> Article:
    return Article(
        id=row[0],
        title=row[1],
        content=row[2],
        html_content=row[3],
        created_at=row[4],
        updated_at=row[5],
        published_at=row[6],
        draft_media_id=row[7]
    )


def create_article(title: str, content: str, html_content: str) -> Article:
    """创建文章"""
    article_id = uuid.uuid4().hex[:8]
    now = datetime.now().isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO articles (id, title, content, html_content, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (article_id, title, content, html_content, now, now)
    )
    conn.commit()
    conn.close()

    return Article(
        id=article_id,
        title=title,
        content=content,
        html_content=html_content,
        created_at=now,
        updated_at=now
    )


def get_article(article_id: str) -> Optional[Article]:
    """获取文章"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "SELECT * FROM articles WHERE id = ?",
        (article_id,)
    )
    row = cursor.fetchone()
    conn.close()

    if row:
        return _row_to_article(row)
    return None


def update_article(
    article_id: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
    html_content: Optional[str] = None
) -> Optional[Article]:
    """更新文章"""
    article = get_article(article_id)
    if not article:
        return None

    new_title = title if title is not None else article.title
    new_content = content if content is not None else article.content
    new_html = html_content if html_content is not None else article.html_content
    now = datetime.now().isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        UPDATE articles
        SET title = ?, content = ?, html_content = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_title, new_content, new_html, now, article_id)
    )
    conn.commit()
    conn.close()

    return get_article(article_id)


def mark_published(article_id: str, draft_media_id: str) -> Optional[Article]:
    """标记文章已发布"""
    now = datetime.now().isoformat()

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        UPDATE articles
        SET published_at = ?, draft_media_id = ?
        WHERE id = ?
        """,
        (now, draft_media_id, article_id)
    )
    conn.commit()
    conn.close()

    return get_article(article_id)


def list_articles() -> list[Article]:
    """列出所有文章"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "SELECT * FROM articles ORDER BY updated_at DESC"
    )
    rows = cursor.fetchall()
    conn.close()

    return [_row_to_article(row) for row in rows]
