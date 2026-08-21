import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.models import Post

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot"


def _to_beijing(dt: Optional[datetime]) -> Optional[datetime]:
    """China has no DST, so UTC+8 is a constant offset (avoids tzdata dep)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc) + timedelta(hours=8)


def format_message(post: Post, matched: list[str]) -> str:
    """Build the Telegram notification text. No secrets are included."""
    beijing = _to_beijing(post.published_at)
    time_str = beijing.strftime("%Y-%m-%d %H:%M:%S") if beijing else "未知"
    lines = [
        "🚨 NodeSeek 关键词提醒",
        "",
        f"关键词：{', '.join(matched)}",
        "",
        f"标题：{post.title}",
    ]
    if post.author:
        lines.append(f"作者：{post.author}")
    if post.category:
        lines.append(f"分类：{post.category}")
    lines.append(f"时间：{time_str}")
    lines.append(f"链接：{post.url}")
    return "\n".join(lines)


class TelegramError(Exception):
    """Raised when a Telegram send fails (network, timeout, or API error)."""


async def send_message(
    token: str, chat_id: str, text: str, timeout: float = 10.0
) -> None:
    """Send a plain-text message via the Telegram Bot API.

    Raises TelegramError on any failure; the caller must NOT let this crash
    the worker (the post stays pending and will be retried next poll).
    """
    url = f"{API_BASE}{token}/sendMessage"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0)
        ) as client:
            resp = await client.post(
                url,
                json={"chat_id": chat_id, "text": text},
            )
    except httpx.TimeoutException as e:
        raise TelegramError(f"timeout: {e}") from e
    except httpx.HTTPError as e:
        raise TelegramError(f"request error: {e}") from e

    if resp.status_code != 200:
        raise TelegramError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except Exception:
        raise TelegramError("invalid JSON response from Telegram")
    if not data.get("ok"):
        raise TelegramError(f"api error: {data}")
