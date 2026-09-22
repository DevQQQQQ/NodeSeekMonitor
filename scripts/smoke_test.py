"""Offline smoke test for NodeSeek Monitor.

Validates, without any network or real Telegram calls:
  - RSS parsing (real NodeSeek field mapping)
  - timezone-less pubDate is read as UTC, not as host-local time
  - keyword matching (case-insensitive, multi-keyword, single send)
  - first-run silence (baseline posts never notified)
  - baseline gate: a failed startup fetch must not open the notification path
  - idempotent dedup (same post never notified twice)
  - Telegram failure -> retry on next poll
  - matched_keywords column is refreshed when KEYWORDS changes
  - already-notified posts are not re-logged on every poll
  - Telegram message format (Beijing time, matched keywords, link)
  - the bot token never appears in a raised TelegramError

Run from the project root:  python scripts/smoke_test.py
"""
import asyncio
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import telegram as tg_mod  # noqa: E402
from app.config import Config  # noqa: E402
from app.database import (  # noqa: E402
    get_row,
    init_db,
    is_baseline_established,
)
from app.main import ensure_baseline, establish_baseline, process_post  # noqa: E402
from app.matcher import match_keywords  # noqa: E402
from app.models import Post  # noqa: E402
from app.parser import _parse_published, parse_feed  # noqa: E402
from app.rss_client import RSSFetchError  # noqa: E402
from app.telegram import TelegramError, format_message, send_message  # noqa: E402

# Realistic fixture modeled on the actual NodeSeek RSS 2.0 structure.
RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
<channel>
<title>NodeSeek</title>
<lastBuildDate>Fri, 21 Aug 2026 00:59:57 GMT</lastBuildDate>
<item>
<title><![CDATA[CloudCone 新套餐补货 1G 内存]]></title>
<description><![CDATA[CloudCone 今天上了新款，性价比不错]]></description>
<link>https://www.nodeseek.com/post-900001-1</link>
<guid isPermaLink="false">900001</guid>
<category><![CDATA[trade]]></category>
<dc:creator><![CDATA[alice]]></dc:creator>
<pubDate>Fri, 21 Aug 2026 00:59:57 GMT</pubDate>
</item>
<item>
<title><![CDATA[DMIT 香港节点延迟测试]]></title>
<description><![CDATA[求问 DMIT 香港表现]]></description>
<link>https://www.nodeseek.com/post-900002-1</link>
<guid isPermaLink="false">900002</guid>
<category><![CDATA[info]]></category>
<dc:creator><![CDATA[bob]]></dc:creator>
<pubDate>Fri, 21 Aug 2026 00:58:00 GMT</pubDate>
</item>
<item>
<title><![CDATA[普通灌水帖 今天天气真好]]></title>
<description><![CDATA[随便聊聊]]></description>
<link>https://www.nodeseek.com/post-900003-1</link>
<guid isPermaLink="false">900003</guid>
<category><![CDATA[daily]]></category>
<dc:creator><![CDATA[carol]]></dc:creator>
<pubDate>Fri, 21 Aug 2026 00:57:00 GMT</pubDate>
</item>
</channel>
</rss>
"""


def make_cfg(keywords, **kw):
    kw.setdefault("telegram_bot_token", "dummy")  # enables telegram path (send mocked)
    kw.setdefault("telegram_chat_id", "12345")
    return Config(keywords=keywords, **kw)


def make_post(post_id, title, description, author, category):
    return Post(
        post_id=post_id,
        title=title,
        url=f"https://www.nodeseek.com/post-{post_id}-1",
        description=description,
        author=author,
        category=category,
        published_at=datetime(2026, 8, 21, 1, 0, 0, tzinfo=timezone.utc),
    )


class TempDB:
    """Context manager yielding an isolated, throwaway SQLite connection."""

    async def __aenter__(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            self.path = tf.name
        self.conn = await init_db(self.path)
        return self.conn

    async def __aexit__(self, *exc):
        await self.conn.close()
        os.unlink(self.path)


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


async def test_parsing():
    posts = parse_feed(RSS_FIXTURE)
    assert len(posts) == 3, f"expected 3 posts, got {len(posts)}"
    p = posts[0]
    assert p.post_id == "900001", p.post_id
    assert "CloudCone" in p.title
    assert p.author == "alice"
    assert p.category == "trade"
    assert p.published_at is not None and p.published_at.tzinfo is not None
    print("[PASS] parse_feed maps guid/title/author/category/pubDate correctly")


async def test_naive_pubdate_treated_as_utc():
    """A pubDate without a timezone must be read as UTC, not as host-local time."""

    class FakeEntry:
        published_parsed = None

        def __init__(self, published):
            self.published = published

    naive = _parse_published(FakeEntry("Fri, 21 Aug 2026 00:59:57"))
    assert naive is not None, "timezone-less pubDate should still parse"
    assert naive == datetime(2026, 8, 21, 0, 59, 57, tzinfo=timezone.utc), (
        f"timezone-less pubDate was shifted by the host UTC offset: {naive}"
    )
    aware = _parse_published(FakeEntry("Fri, 21 Aug 2026 00:59:57 GMT"))
    assert aware == datetime(2026, 8, 21, 0, 59, 57, tzinfo=timezone.utc), aware
    print("[PASS] naive pubDate read as UTC (host TZ no longer leaks into results)")


async def test_matching():
    posts = parse_feed(RSS_FIXTURE)
    by_id = {p.post_id: p for p in posts}
    assert match_keywords(by_id["900001"], ["CloudCone"]) == ["CloudCone"]
    assert match_keywords(by_id["900002"], ["CloudCone", "DMIT"]) == ["DMIT"]
    # case-insensitive
    assert match_keywords(by_id["900001"], ["cloudcone"]) == ["cloudcone"]
    assert match_keywords(by_id["900001"], ["CloudCone", "DMIT"]) == ["CloudCone"]
    assert match_keywords(by_id["900003"], ["CloudCone", "DMIT"]) == []
    print("[PASS] match_keywords: case-insensitive, multi-hit, no false positive")


async def test_first_run_silence_and_dedup():
    cfg = make_cfg(["CloudCone", "DMIT"])
    sent = []
    async with TempDB() as conn:
        posts = parse_feed(RSS_FIXTURE)
        await establish_baseline(conn, posts, cfg)

        # Same posts again -> must NOT notify (baseline silence)
        with patch("app.main.send_message", side_effect=lambda *a, **k: sent.append(a)):
            for p in posts:
                await process_post(p, cfg, conn)
        assert len(sent) == 0, f"baseline posts must not notify, got {len(sent)}"

        # Brand-new matching post -> notify once; re-run -> no duplicate
        new_post = make_post(
            "900009", "CloudCone 618 大促", "CloudCone 优惠", "dave", "trade"
        )
        with patch("app.main.send_message", side_effect=lambda *a, **k: sent.append(a)):
            await process_post(new_post, cfg, conn)
            await process_post(new_post, cfg, conn)
        assert len(sent) == 1, f"new post should notify once, got {len(sent)}"
        row = await get_row(conn, "900009")
        assert row[1] is not None, "notified_at must be set after success"
    print("[PASS] first-run silence + idempotent dedup (notify exactly once)")


async def test_baseline_gate_blocks_flood():
    """A failed startup fetch must not let the next poll push historical posts."""
    cfg = make_cfg(["CloudCone"])
    sent = []
    async with TempDB() as conn:
        # Startup fetch fails -> baseline NOT established -> gate stays closed
        with patch("app.main.fetch_rss", side_effect=RSSFetchError("network down")):
            ready = await ensure_baseline(conn, cfg)
        assert ready is False, (
            "baseline must not be considered established on fetch failure"
        )
        assert not await is_baseline_established(conn)

        # The poll loop would not run at all here (wait_for_baseline blocks), so
        # nothing can be notified. Verify the gate is what stops it.
        posts = parse_feed(RSS_FIXTURE)
        with (
            patch("app.main.send_message", side_effect=lambda *a, **k: sent.append(a)),
            patch("app.main.fetch_rss", side_effect=RSSFetchError("network down")),
        ):
            await ensure_baseline(conn, cfg)
        assert len(sent) == 0, "no notification may happen while the gate is closed"

        # Feed recovers -> baseline is established from the *current* feed and
        # those historical posts are still never notified.
        with patch("app.main.fetch_rss", return_value=RSS_FIXTURE):
            ready = await ensure_baseline(conn, cfg)
        assert ready is True
        assert await is_baseline_established(conn)

        with patch("app.main.send_message", side_effect=lambda *a, **k: sent.append(a)):
            for p in posts:
                await process_post(p, cfg, conn)
        assert len(sent) == 0, f"historical posts flooded after recovery: {len(sent)}"
    print(
        "[PASS] baseline gate: failed startup fetch cannot trigger a "
        "historical-post flood"
    )


async def test_failure_retry():
    cfg = make_cfg(["CloudCone"])
    sent = []
    async with TempDB() as conn:
        post = make_post("900111", "CloudCone 故障", "CloudCone down", "eve", "info")
        # First poll: Telegram fails -> stays pending (notified_at NULL)
        with patch("app.main.send_message", side_effect=TelegramError("boom")):
            await process_post(post, cfg, conn)
        row = await get_row(conn, "900111")
        assert row[1] is None, "failed send must leave notified_at NULL"

        # Next poll: succeeds -> notifies once, no duplicate
        with patch("app.main.send_message", side_effect=lambda *a, **k: sent.append(a)):
            await process_post(post, cfg, conn)
            await process_post(post, cfg, conn)
        assert len(sent) == 1, f"retry should send once, got {len(sent)}"
    print("[PASS] Telegram failure leaves post pending and retries next poll")


async def test_matched_keywords_refresh():
    """Changing KEYWORDS must update the stored keyword list of a seen post."""
    async with TempDB() as conn:
        post = make_post(
            "900222", "CloudCone 与 DMIT 对比", "CloudCone vs DMIT", "f", "info"
        )
        with patch("app.main.send_message", side_effect=lambda *a, **k: None):
            await process_post(post, make_cfg(["CloudCone"]), conn)
        assert (await get_row(conn, "900222"))[0] == "CloudCone"

        # user adds DMIT -> stored keywords must follow
        await process_post(post, make_cfg(["CloudCone", "DMIT"]), conn)
        assert (await get_row(conn, "900222"))[0] == "CloudCone,DMIT", (
            "matched_keywords went stale after KEYWORDS changed"
        )
    print("[PASS] matched_keywords refreshed when KEYWORDS changes")


async def test_no_repeat_logging():
    """An already-notified post must not re-log its match on every poll."""
    cfg = make_cfg(["CloudCone"])
    capture = LogCapture()
    target = logging.getLogger("nodeseek")
    target.addHandler(capture)
    target.setLevel(logging.INFO)
    try:
        async with TempDB() as conn:
            post = make_post("900333", "CloudCone 补货", "CloudCone x", "g", "trade")
            with patch("app.main.send_message", side_effect=lambda *a, **k: None):
                for _ in range(5):  # post lingers in the feed across 5 polls
                    await process_post(post, cfg, conn)
    finally:
        target.removeHandler(capture)
    matched_logs = [m for m in capture.messages if "Matched keyword" in m]
    assert len(matched_logs) == 1, (
        f"expected 1 match log for 5 polls, got {len(matched_logs)}"
    )
    print("[PASS] already-notified posts are not re-logged on every poll")


async def test_token_never_in_error():
    """The bot token must not survive into a raised TelegramError."""
    token = "123456:SECRET-TOKEN-VALUE"
    original = tg_mod.API_BASE
    tg_mod.API_BASE = "http://127.0.0.1:9/bot"  # connection refused, no external net
    try:
        await send_message(token, "999", "hi")
        raise AssertionError("send_message should have raised")
    except TelegramError as e:
        assert token not in str(e), f"token leaked into error text: {e}"
    finally:
        tg_mod.API_BASE = original
    print("[PASS] bot token never appears in a TelegramError")


async def test_message_format():
    post = Post(
        post_id="900001",
        title="CloudCone 新套餐补货",
        url="https://www.nodeseek.com/post-900001-1",
        description="x",
        author="alice",
        category="trade",
        published_at=datetime(2026, 8, 21, 0, 59, 57, tzinfo=timezone.utc),
    )
    msg = format_message(post, ["CloudCone"])
    assert "关键词：CloudCone" in msg
    assert "标题：CloudCone 新套餐补货" in msg
    assert "作者：alice" in msg
    assert "分类：trade" in msg
    assert "时间：2026-08-21 08:59:57" in msg, msg  # GMT+8
    assert "链接：https://www.nodeseek.com/post-900001-1" in msg
    assert "dummy" not in msg and "your_bot_token" not in msg
    print(
        "[PASS] format_message: Beijing time, matched kw, "
        "author/category/link, no secrets"
    )


async def main_test():
    await test_parsing()
    await test_naive_pubdate_treated_as_utc()
    await test_matching()
    await test_first_run_silence_and_dedup()
    await test_baseline_gate_blocks_flood()
    await test_failure_retry()
    await test_matched_keywords_refresh()
    await test_no_repeat_logging()
    await test_token_never_in_error()
    await test_message_format()
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main_test())
