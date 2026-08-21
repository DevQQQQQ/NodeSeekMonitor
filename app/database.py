import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from app.models import Post

logger = logging.getLogger(__name__)

# Sentinel stored in notified_at for posts that existed at first-run baseline.
# It means "processed at startup, intentionally not notified" so they are never
# re-evaluated for notification. Real sends store an ISO timestamp instead.
BASELINE_MARKER = "BASELINE"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db(path: str) -> aiosqlite.Connection:
    """Open the SQLite database and create the seen_posts table if missing."""
    conn = await aiosqlite.connect(path)
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT UNIQUE,
            title TEXT,
            url TEXT,
            description TEXT,
            author TEXT,
            category TEXT,
            published_at TEXT,
            matched_keywords TEXT DEFAULT '',
            notified_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    await conn.commit()
    return conn


async def is_seen(conn: aiosqlite.Connection, post_id: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM seen_posts WHERE post_id = ?", (post_id,)
    ) as cur:
        return await cur.fetchone() is not None


async def get_row(
    conn: aiosqlite.Connection, post_id: str
) -> Optional[tuple]:
    """Return (matched_keywords, notified_at) for a post, or None."""
    async with conn.execute(
        "SELECT matched_keywords, notified_at FROM seen_posts WHERE post_id = ?",
        (post_id,),
    ) as cur:
        return await cur.fetchone()


async def mark_seen(
    conn: aiosqlite.Connection,
    post: Post,
    matched_keywords: str = "",
    notified_at: Optional[str] = None,
) -> None:
    """Insert a post as seen (idempotent)."""
    await conn.execute(
        """
        INSERT OR IGNORE INTO seen_posts
            (post_id, title, url, description, author, category,
             published_at, matched_keywords, notified_at, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            post.post_id,
            post.title,
            post.url,
            post.description,
            post.author,
            post.category,
            post.published_at.isoformat() if post.published_at else None,
            matched_keywords,
            notified_at,
            _now_iso(),
        ),
    )
    await conn.commit()


async def mark_notified(
    conn: aiosqlite.Connection, post_id: str, notified_at: str
) -> None:
    """Record a successful Telegram notification."""
    await conn.execute(
        "UPDATE seen_posts SET notified_at = ? WHERE post_id = ?",
        (notified_at, post_id),
    )
    await conn.commit()
