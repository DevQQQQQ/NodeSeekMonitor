from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Post:
    """Unified post representation produced by the parser.

    `post_id` comes from the RSS <guid> (a stable numeric id on NodeSeek).
    `published_at` is stored as a timezone-aware UTC datetime.
    Fields that the feed does not provide are left as None/empty.
    """

    post_id: str
    title: str
    url: str
    description: str = ""
    author: Optional[str] = None
    category: Optional[str] = None
    published_at: Optional[datetime] = None  # always UTC when present
