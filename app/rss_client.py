import httpx

# A descriptive User-Agent is REQUIRED: NodeSeek RSS returns 403 without one.
USER_AGENT = "nodeSeekMonitor/1.0 (+https://www.nodeseek.com)"


class RSSFetchError(Exception):
    """Raised when the RSS feed cannot be fetched or returns a non-200 status."""


async def fetch_rss(url: str, timeout: float = 15.0) -> str:
    """Fetch the raw RSS content.

    Raises RSSFetchError on network failure, timeout, or non-200 status.
    The caller is responsible for catching this and continuing to the next poll.
    """
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(url)
    except httpx.TimeoutException as e:
        raise RSSFetchError(f"request timeout: {e}") from e
    except httpx.HTTPError as e:
        raise RSSFetchError(f"request error: {e}") from e

    if resp.status_code != 200:
        raise RSSFetchError(f"HTTP {resp.status_code}")
    return resp.text
