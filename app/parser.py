import calendar
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser

from app.models import Post

logger = logging.getLogger(__name__)

# NodeSeek post links look like https://www.nodeseek.com/post-885506-1
_LINK_ID_RE = re.compile(r"post-(\d+)")


def _extract_post_id(entry) -> str:
    """Prefer the stable <guid> (numeric id); fall back to the id in the URL,
    then to the URL itself. Never use the title as an identifier."""
    guid = getattr(entry, "guid", None)
    if guid and str(guid).strip():
        return str(guid).strip()
    link = getattr(entry, "link", None) or ""
    m = _LINK_ID_RE.search(link)
    if m:
        return m.group(1)
    return link


def _parse_published(entry) -> Optional[datetime]:
    """Return a timezone-aware UTC datetime from feedparser's parsed time."""
    parsed = getattr(entry, "published_parsed", None)
    if parsed:
        try:
            return datetime.fromtimestamp(
                calendar.timegm(parsed), tz=timezone.utc
            )
        except (ValueError, OverflowError):
            pass
    raw = getattr(entry, "published", None)
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None
    return None


def _get_author(entry) -> Optional[str]:
    author = getattr(entry, "author", None)
    if author:
        return author
    authors = getattr(entry, "authors", None)
    if isinstance(authors, list) and authors:
        first = authors[0]
        if isinstance(first, dict) and first.get("name"):
            return first["name"]
    return None


def _get_category(entry) -> Optional[str]:
    category = getattr(entry, "category", None)
    if category:
        return category
    tags = getattr(entry, "tags", None)
    if isinstance(tags, list):
        terms = [t.get("term") for t in tags if isinstance(t, dict) and t.get("term")]
        if terms:
            return terms[0]
    return None


def parse_feed(content: str) -> list[Post]:
    """Parse raw RSS/Atom content into a list of Post objects.

    Raises ValueError on a malformed feed with no usable entries.
    """
    try:
        parsed = feedparser.parse(content)
    except Exception as e:  # feedparser is defensive, but never trust inputs
        raise ValueError(f"feedparser failed: {e}") from e

    if parsed.bozo and not parsed.entries:
        raise ValueError(f"malformed RSS: {parsed.get('bozo_exception')}")

    posts: list[Post] = []
    for entry in parsed.entries:
        title = (getattr(entry, "title", "") or "").strip()
        if not title:
            continue
        posts.append(
            Post(
                post_id=_extract_post_id(entry),
                title=title,
                url=getattr(entry, "link", "") or "",
                description=getattr(entry, "description", "") or "",
                author=_get_author(entry),
                category=_get_category(entry),
                published_at=_parse_published(entry),
            )
        )
    return posts
