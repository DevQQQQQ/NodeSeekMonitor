import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

DEFAULT_RSS_URL = "https://rss.nodeseek.com/"
DEFAULT_POLL_INTERVAL = 30
DEFAULT_KEYWORDS = "CloudCone"
DEFAULT_DATABASE_PATH = "/app/data/nodeseek.db"
DEFAULT_LOG_LEVEL = "INFO"


def _split_keywords(raw: str) -> list[str]:
    """Split a comma-separated keyword string, trimming spaces, dropping empties."""
    return [k.strip() for k in raw.split(",") if k.strip()]


@dataclass
class Config:
    rss_url: str = DEFAULT_RSS_URL
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL
    keywords: list[str] = field(default_factory=lambda: [DEFAULT_KEYWORDS])
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    database_path: str = DEFAULT_DATABASE_PATH
    log_level: str = DEFAULT_LOG_LEVEL

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token) and bool(self.telegram_chat_id)


def load_config() -> Config:
    """Read configuration from environment variables with sane defaults."""
    raw_interval = os.getenv("POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL))
    try:
        poll_interval = int(raw_interval)
    except (ValueError, TypeError):
        poll_interval = DEFAULT_POLL_INTERVAL
    if poll_interval <= 0:
        poll_interval = DEFAULT_POLL_INTERVAL

    keywords = _split_keywords(os.getenv("KEYWORDS", DEFAULT_KEYWORDS))
    if not keywords:
        keywords = _split_keywords(DEFAULT_KEYWORDS)

    log_level = os.getenv("LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()

    return Config(
        rss_url=os.getenv("RSS_URL", DEFAULT_RSS_URL),
        poll_interval_seconds=poll_interval,
        keywords=keywords,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        database_path=os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH),
        log_level=log_level,
    )
