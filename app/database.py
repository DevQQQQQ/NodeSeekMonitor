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

# meta key: "1" once the first-run baseline has been successfully recorded.
# While it is absent, notifications must stay disabled (see app/main.py).
BASELINE_FLAG_KEY = "baseline_established"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db(path: str) -> aiosqlite.Connection:
    """Open the SQLite database and create the tables if missing."""
    conn = await aiosqlite.connect(path)

    # Detected *before* creating it, so we can tell "database from an older
    # version" apart from "this version crashed mid-baseline".
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ) as cur:
        meta_existed = await cur.fetchone() is not None

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
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    await conn.commit()

    # Migration for databases written by versions that had no meta table: a
    # non-empty seen_posts means a baseline was already recorded, so adopt it
    # instead of re-baselining and risking a duplicate notification burst.
    if not meta_existed:
        async with conn.execute("SELECT COUNT(1) FROM seen_posts") as cur:
            row = await cur.fetchone()
        if row and row[0] > 0:
            await set_baseline_established(conn)
            logger.info(
                "Adopted existing database (%d rows) as an established baseline",
                row[0],
            )

    return conn


async def get_meta(conn: aiosqlite.Connection, key: str) -> Optional[str]:
    async with conn.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def set_meta(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await conn.commit()


async def is_baseline_established(conn: aiosqlite.Connection) -> bool:
    return await get_meta(conn, BASELINE_FLAG_KEY) == "1"


async def set_baseline_established(conn: aiosqlite.Connection) -> None:
    await set_meta(conn, BASELINE_FLAG_KEY, "1")


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


async def update_matched_keywords(
    conn: aiosqlite.Connection, post_id: str, matched_keywords: str
) -> None:
    """Refresh the stored keyword list for an already-seen post.

    `mark_seen` uses INSERT OR IGNORE, so a post first recorded under an older
    KEYWORDS value would otherwise keep stale keywords forever.
    """
    await conn.execute(
        "UPDATE seen_posts SET matched_keywords = ? WHERE post_id = ?",
        (matched_keywords, post_id),
    )
    await conn.commit()
