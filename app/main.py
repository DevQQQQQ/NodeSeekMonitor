import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from app.config import Config, load_config
from app.database import (
    BASELINE_MARKER,
    get_row,
    init_db,
    is_seen,
    mark_notified,
    mark_seen,
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

    # Keyword matched
    logger.info("Matched keyword=%s | %s", ",".join(matched), post.title)

    if not seen:
        await mark_seen(conn, post, matched_keywords=",".join(matched))
        is_new = True
    else:
        is_new = False

    if not cfg.telegram_enabled:
        logger.warning(
            "Telegram not configured; skipping notify for: %s", post.title
        )
        return is_new, False

    row = await get_row(conn, post.post_id) if seen else None
    already_notified = bool(row and row[1])
    if already_notified:
        return is_new, False

    notified = await try_notify(post, matched, cfg, conn)
    return is_new, notified


async def run(cfg: Config) -> None:
    ensure_data_dir(cfg.database_path)
    conn = await init_db(cfg.database_path)

    # --- First-run baseline (silent) ---
    try:
        content = await fetch_rss(cfg.rss_url)
        baseline_posts = parse_feed(content)
    except (RSSFetchError, ValueError) as e:
        logger.error("RSS fetch/parse failed during startup: %s", e)
        baseline_posts = []

    if baseline_posts:
        for p in baseline_posts:
            if not await is_seen(conn, p.post_id):
                matched = match_keywords(p, cfg.keywords)
                # Mark baseline-matched posts as already "handled" so they are
                # never notified later (first-run silence).
                marker = BASELINE_MARKER if matched else None
                await mark_seen(
                    conn, p, matched_keywords=",".join(matched), notified_at=marker
                )
        logger.info(
            "First-run baseline established, posts=%d (no notifications sent)",
            len(baseline_posts),
        )
    else:
        logger.warning("No baseline posts; monitoring from next polls")

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

    # --- Poll loop ---
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
            logger.error("RSS parse failed: %s", e)
        except Exception as e:  # never let one bad poll kill the worker
            logger.error("Unexpected error in poll loop: %s", e)

        await asyncio.sleep(cfg.poll_interval_seconds)


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
