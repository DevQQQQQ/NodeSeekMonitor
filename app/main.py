import asyncio
import contextlib
import logging
import os
import sys
from datetime import datetime, timezone

from app.config import Config, load_config
from app.database import (
    BASELINE_MARKER,
    get_row,
    init_db,
    is_baseline_established,
    is_seen,
    mark_notified,
    mark_seen,
    set_baseline_established,
    update_matched_keywords,
)
from app.matcher import match_keywords
from app.parser import parse_feed
from app.rss_client import RSSFetchError, fetch_rss
from app.telegram import TelegramError, format_message, send_message

logger = logging.getLogger("nodeseek")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # Keep httpx's own "HTTP Request" lines out of our concise logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def ensure_data_dir(db_path: str) -> None:
    d = os.path.dirname(db_path)
    if d:
        os.makedirs(d, exist_ok=True)


async def try_notify(post, matched: list[str], cfg: Config, conn) -> bool:
    """Attempt to send a Telegram notification; return True on success."""
    msg = format_message(post, matched)
    try:
        await send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, msg)
    except TelegramError as e:
        logger.error("Telegram notification failed: %s", e)
        return False
    await mark_notified(conn, post.post_id, datetime.now(timezone.utc).isoformat())
    logger.info("Telegram notification sent: %s", post.title)
    return True


async def establish_baseline(conn, posts, cfg: Config) -> None:
    """Record every post currently in the feed as seen, without notifying.

    Matching posts get the BASELINE sentinel in `notified_at` so they are never
    notified later (first-run silence). Non-matching ones stay pending, so they
    can still be pushed if KEYWORDS later changes to match them.
    """
    for p in posts:
        if await is_seen(conn, p.post_id):
            continue
        matched = match_keywords(p, cfg.keywords)
        marker = BASELINE_MARKER if matched else None
        await mark_seen(
            conn, p, matched_keywords=",".join(matched), notified_at=marker
        )


async def ensure_baseline(conn, cfg: Config) -> bool:
    """Establish the first-run baseline once; True when it is in place.

    A failed startup fetch must NOT be treated as an empty baseline: that would
    make the next successful poll look like every post in the feed is brand new
    and would push a burst of historical posts.
    """
    if await is_baseline_established(conn):
        return True

    try:
        content = await fetch_rss(cfg.rss_url)
        posts = parse_feed(content)
    except (RSSFetchError, ValueError) as e:
        logger.error("RSS fetch/parse failed during startup baseline: %s", e)
        return False

    if not posts:
        logger.warning("RSS returned no usable posts; baseline not established")
        return False

    await establish_baseline(conn, posts, cfg)
    await set_baseline_established(conn)
    logger.info(
        "First-run baseline established, posts=%d (no notifications sent)",
        len(posts),
    )
    return True


async def wait_for_baseline(conn, cfg: Config) -> None:
    """Block until the baseline exists. Notifications stay off until then."""
    while not await ensure_baseline(conn, cfg):
        logger.warning(
            "Notifications stay disabled until the baseline is established; "
            "retrying in %ds",
            cfg.poll_interval_seconds,
        )
        await asyncio.sleep(cfg.poll_interval_seconds)


async def process_post(post, cfg: Config, conn) -> tuple[bool, bool]:
    """Process a single post.

    Returns (is_new, notified).
    Rules:
      - No keyword match -> just record as seen (silent).
      - Match + never seen -> record seen (with matched keywords) and notify.
      - Match + seen but not yet notified (e.g. previous send failed) -> retry.
      - Match + already notified -> skip (idempotent, no duplicate).
    """
    seen = await is_seen(conn, post.post_id)
    matched = match_keywords(post, cfg.keywords)

    if not matched:
        if not seen:
            await mark_seen(conn, post)
            return True, False
        return False, False

    # --- Keyword matched ---
    if not seen:
        await mark_seen(conn, post, matched_keywords=",".join(matched))
        is_new = True
        notified_at = None
    else:
        is_new = False
        row = await get_row(conn, post.post_id)
        notified_at = row[1] if row else None
        # KEYWORDS may have changed since this post was first recorded, and
        # mark_seen (INSERT OR IGNORE) never updates an existing row, so refresh
        # the stored keywords here to keep the column truthful.
        stored = row[0] if row else ""
        if stored != ",".join(matched):
            await update_matched_keywords(conn, post.post_id, ",".join(matched))

    if not cfg.telegram_enabled:
        if is_new:
            logger.warning(
                "Telegram not configured; matched post not notified: %s",
                post.title,
            )
        return is_new, False

    if notified_at:
        # Already handled (a real send, or the BASELINE sentinel) -> stay quiet
        # instead of re-logging the match on every single poll.
        return is_new, False

    logger.info("Matched keyword=%s | %s", ",".join(matched), post.title)
    notified = await try_notify(post, matched, cfg, conn)
    return is_new, notified


async def run(cfg: Config) -> None:
    ensure_data_dir(cfg.database_path)
    conn = await init_db(cfg.database_path)
    try:
        # --- Phase 1: establish the baseline before any notification is possible ---
        await wait_for_baseline(conn, cfg)

        logger.info(
            "Monitoring keywords=%s interval=%ds",
            ",".join(cfg.keywords),
            cfg.poll_interval_seconds,
        )
        if not cfg.telegram_enabled:
            logger.warning(
                "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; "
                "running in monitor-only mode (no messages sent)"
            )

        # --- Phase 2: poll loop ---
        consecutive_failures = 0
        while True:
            try:
                content = await fetch_rss(cfg.rss_url)
                posts = parse_feed(content)
                consecutive_failures = 0
                logger.info("RSS fetch success, items=%d", len(posts))

                new_count = 0
                sent_count = 0
                for post in posts:
                    is_new, notified = await process_post(post, cfg, conn)
                    if is_new:
                        new_count += 1
                    if notified:
                        sent_count += 1

                logger.info("New posts=%d, sent=%d", new_count, sent_count)
                if new_count == 0:
                    logger.info("No new matching posts")
            except RSSFetchError as e:
                consecutive_failures += 1
                lvl = logging.ERROR if consecutive_failures >= 3 else logging.WARNING
                logger.log(
                    lvl,
                    "RSS request failed: %s (consecutive=%d)",
                    e,
                    consecutive_failures,
                )
            except ValueError as e:
                consecutive_failures += 1
                lvl = logging.ERROR if consecutive_failures >= 3 else logging.WARNING
                logger.log(
                    lvl,
                    "RSS parse failed: %s (consecutive=%d)",
                    e,
                    consecutive_failures,
                )
            except Exception as e:  # never let one bad poll kill the worker
                consecutive_failures += 1
                logger.error(
                    "Unexpected error in poll loop: %s (consecutive=%d)",
                    e,
                    consecutive_failures,
                )

            await asyncio.sleep(cfg.poll_interval_seconds)
    finally:
        # Every write is committed eagerly, so an interrupted close loses nothing.
        with contextlib.suppress(Exception):
            await conn.close()


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
