"""Offline smoke test for NodeSeek Monitor.

Validates, without any network or real Telegram calls:
  - RSS parsing (real NodeSeek field mapping)
  - keyword matching (case-insensitive, multi-keyword, single send)
  - first-run silence (baseline posts never notified)
  - idempotent dedup (same post never notified twice)
  - Telegram failure -> retry on next poll
  - Telegram message format (Beijing time, matched keywords, link)

Run from the project root:  python scripts/smoke_test.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config  # noqa: E402
from app.database import BASELINE_MARKER, get_row, init_db, is_seen, mark_seen  # noqa: E402
from app.parser import parse_feed  # noqa: E402
from app.matcher import match_keywords  # noqa: E402
from app.models import Post  # noqa: E402
from app.telegram import TelegramError, format_message  # noqa: E402
from app.main import process_post  # noqa: E402


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


def make_cfg(keywords):
    return Config(
        keywords=keywords,
        telegram_bot_token="dummy",  # enables telegram path (send is mocked)
        telegram_chat_id="12345",
    )


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


async def test_matching():
    posts = parse_feed(RSS_FIXTURE)
    by_id = {p.post_id: p for p in posts}
    assert match_keywords(by_id["900001"], ["CloudCone"]) == ["CloudCone"]
    assert match_keywords(by_id["900002"], ["CloudCone", "DMIT"]) == ["DMIT"]
    assert match_keywords(by_id["900001"], ["cloudcone"]) == ["cloudcone"]  # case-insensitive
    assert match_keywords(by_id["900001"], ["CloudCone", "DMIT"]) == ["CloudCone"]
    assert match_keywords(by_id["900003"], ["CloudCone", "DMIT"]) == []
    print("[PASS] match_keywords: case-insensitive, multi-hit, no false positive")


async def test_first_run_silence_and_dedup():
    cfg = make_cfg(["CloudCone", "DMIT"])
    sent = []
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name
    conn = await init_db(db_path)
    try:
        posts = parse_feed(RSS_FIXTURE)
        # Replicate main.py first-run baseline
        for p in posts:
            if not await is_seen(conn, p.post_id):
                matched = match_keywords(p, cfg.keywords)
                marker = BASELINE_MARKER if matched else None
                await mark_seen(conn, p, matched_keywords=",".join(matched), notified_at=marker)

        # Same posts again -> must NOT notify (baseline silence)
        with patch("app.main.send_message", side_effect=lambda *a, **k: sent.append(a)):
            for p in posts:
                await process_post(p, cfg, conn)
        assert len(sent) == 0, f"baseline posts must not notify, got {len(sent)}"

        # Brand-new matching post -> notify once; re-run -> no duplicate
        new_post = make_post("900009", "CloudCone 618 大促", "CloudCone 优惠", "dave", "trade")
        with patch("app.main.send_message", side_effect=lambda *a, **k: sent.append(a)):
            await process_post(new_post, cfg, conn)
            await process_post(new_post, cfg, conn)
        assert len(sent) == 1, f"new post should notify once, got {len(sent)}"
        row = await get_row(conn, "900009")
        assert row[1] is not None, "notified_at must be set after success"
        print("[PASS] first-run silence + idempotent dedup (notify exactly once)")
    finally:
        await conn.close()
        os.unlink(db_path)


async def test_failure_retry():
    cfg = make_cfg(["CloudCone"])
    sent = []
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name
    conn = await init_db(db_path)
    try:
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
    finally:
        await conn.close()
        os.unlink(db_path)


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
    print("[PASS] format_message: Beijing time, matched kw, author/category/link, no secrets")


async def main_test():
    await test_parsing()
    await test_matching()
    await test_first_run_silence_and_dedup()
    await test_failure_retry()
    await test_message_format()
    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main_test())
