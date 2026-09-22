import re
from html import unescape

from app.models import Post

_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(text: str) -> str:
    """Strip HTML tags and decode entities so matching works on plain text."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def match_keywords(post: Post, keywords: list[str]) -> list[str]:
    """Return the list of keywords matched by the post (case-insensitive).

    Matching is a plain substring test over `title + " " + description`.
    The returned list preserves the original (configured) keyword casing so it
    can be shown verbatim in the Telegram message. A post matching several
    keywords returns all of them (the caller sends a single message).
    """
    haystack = clean_text(f"{post.title} {post.description}").lower()
    matched: list[str] = []
    for kw in keywords:
        if not kw:
            continue
        if kw.lower() in haystack:
            matched.append(kw)
    return matched
