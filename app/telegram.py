from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.models import Post

API_BASE = "https://api.telegram.org/bot"
# China has no DST, so UTC+8 is a constant offset (avoids a tzdata dependency).
BEIJING_TZ = timezone(timedelta(hours=8))


def _to_beijing(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert to Beijing time (UTC+8), keeping the tzinfo honest.

    Returning a datetime whose tzinfo still says UTC would be a trap for any
    later arithmetic, even though strftime() would look right.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING_TZ)


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


def _redact(text: str, secret: str) -> str:
    """Strip the bot token from a message.

    The token travels inside the request URL, so some transport-level error
    strings embed it. Redacting here makes "the token never reaches the logs" an
    enforced property rather than a lucky one.
    """
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


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
        # `from None` keeps the original exception (which may carry the
        # token-bearing URL) out of any traceback rendering.
        raise TelegramError(f"timeout: {_redact(str(e), token)}") from None
    except httpx.HTTPError as e:
        raise TelegramError(f"request error: {_redact(str(e), token)}") from None

    if resp.status_code != 200:
        raise TelegramError(
            f"HTTP {resp.status_code}: {_redact(resp.text[:200], token)}"
        )
    try:
        data = resp.json()
    except Exception:
        raise TelegramError("invalid JSON response from Telegram") from None
    if not data.get("ok"):
        raise TelegramError(f"api error: {_redact(str(data), token)}")
