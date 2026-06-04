"""Shared yt-dlp option and retry helpers."""

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


DEFAULT_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-us,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def parse_cookies_from_browser(value):
    """Convert a CLI-style browser cookie string into yt-dlp API tuple form."""
    if not value:
        return None
    parts = [part.strip() for part in str(value).split(":")]
    while parts and parts[-1] == "":
        parts.pop()
    return tuple(parts) if parts else None


def build_yt_dlp_opts(
    *,
    yt_dlp_cookie_file: Optional[str] = None,
    yt_dlp_cookies_from_browser: Optional[str] = None,
    retries: int = 3,
    socket_timeout: int = 20,
    quiet: bool = True,
    no_warnings: bool = True,
    **overrides,
) -> dict:
    """Build consistent yt-dlp options for metadata and media downloads."""
    opts = {
        "quiet": quiet,
        "no_warnings": no_warnings,
        "retries": retries,
        "fragment_retries": retries,
        "socket_timeout": socket_timeout,
        "http_headers": dict(DEFAULT_HTTP_HEADERS),
    }
    if yt_dlp_cookie_file:
        opts["cookiefile"] = yt_dlp_cookie_file
        if yt_dlp_cookies_from_browser:
            logger.info("yt-dlp cookie file configured; ignoring browser cookie setting")
    elif yt_dlp_cookies_from_browser:
        parsed = parse_cookies_from_browser(yt_dlp_cookies_from_browser)
        if parsed:
            opts["cookiesfrombrowser"] = parsed

    for key, value in overrides.items():
        if value is not None:
            opts[key] = value
    return opts


def run_with_retries(
    label: str,
    action: Callable[[], object],
    *,
    attempts: int = 3,
    shutdown_check: Optional[Callable[[], bool]] = None,
):
    """Run a yt-dlp action with bounded retries and concise diagnostics."""
    last_error = None
    for attempt in range(1, max(1, attempts) + 1):
        if shutdown_check and shutdown_check():
            logger.debug("%s aborted before attempt %d due to shutdown", label, attempt)
            return None
        try:
            result = action()
            if attempt > 1:
                logger.info("%s succeeded on retry %d/%d", label, attempt, attempts)
            return result
        except Exception as exc:  # pragma: no cover - exercised through callers
            last_error = exc
            logger.warning("%s failed on attempt %d/%d: %s", label, attempt, attempts, exc)
            if attempt >= attempts:
                break
            time.sleep(min(2.0, 0.5 * attempt))
    if last_error is not None:
        raise last_error
    return None
