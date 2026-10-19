"""ESPNcricinfo retrieval: a polite fetcher for match reports and player profiles.

The fetcher only takes what the site serves to an honest client:

- It reads robots.txt first and skips any page it disallows. If robots.txt
  itself is refused (401/403), every page is treated as disallowed, as
  Python's standard robots parser does.
- It waits at least `delay` seconds between requests and identifies itself
  with a user agent naming the project.
- It caches every page it gets, so nothing is fetched twice and the context
  database can be rebuilt offline.
- When the site refuses a request (401, 403, 429) it stops asking for the
  rest of the run. It never disguises itself as a browser or works around a
  block.

ESPNcricinfo's terms of use forbid automated collection of its content for AI
tools and databases without written permission, and its bot protection
currently refuses automated requests, so expect FetchRefused unless the site
has granted access.
"""

import hashlib
import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.robotparser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

BASE_URL = "https://www.espncricinfo.com"
USER_AGENT = "CricAssistPro/0.1 (cricket research assistant; +https://github.com/Pratyush415/CricAssist-Pro)"
DEFAULT_DELAY = 5.0  # seconds between requests
REFUSED_STATUSES = (401, 403, 429)

# (url, headers) -> (HTTP status, body text). Swappable for tests.
Opener = Callable[[str, dict], tuple[int, str]]


class FetchRefused(Exception):
    """The site, or its robots.txt, does not allow this request."""


@dataclass
class Page:
    url: str
    html: str
    from_cache: bool


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def match_report_url(match_id: int) -> str:
    """ESPNcricinfo's address for a match, by its id (Cricsheet uses the same ids)."""
    return f"{BASE_URL}/ci/engine/match/{match_id}.html"


def player_profile_url(cricinfo_id: int, name: str) -> str:
    return f"{BASE_URL}/cricketers/{_slug(name)}-{cricinfo_id}"


def urllib_opener(url: str, headers: dict) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.status, response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


class CricinfoClient:
    def __init__(
        self,
        cache_dir: Path,
        delay: float = DEFAULT_DELAY,
        user_agent: str = USER_AGENT,
        opener: Opener = urllib_opener,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cache_dir = Path(cache_dir)
        self.delay = delay
        self.headers = {"User-Agent": user_agent, "Accept": "text/html"}
        self.opener = opener
        self.sleep = sleep
        self.clock = clock
        self._last_request: float | None = None
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self.refused_reason: str | None = None  # set once the site refuses; no more requests after that

    # --- Cache ---------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}.html"

    def cached(self, url: str) -> Page | None:
        path = self._cache_path(url)
        return Page(url, path.read_text("utf-8"), from_cache=True) if path.is_file() else None

    def _save(self, url: str, html: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(url).write_text(html, "utf-8")
        index = self.cache_dir / "index.jsonl"
        with open(index, "a", encoding="utf-8") as f:
            f.write(json.dumps({"url": url, "file": self._cache_path(url).name}) + "\n")

    # --- Network -------------------------------------------------------------

    def _get(self, url: str) -> tuple[int, str]:
        if self._last_request is not None:
            wait = self.delay - (self.clock() - self._last_request)
            if wait > 0:
                self.sleep(wait)
        try:
            return self.opener(url, self.headers)
        finally:
            self._last_request = self.clock()

    def _robots_parser(self) -> urllib.robotparser.RobotFileParser:
        if self._robots is None:
            parser = urllib.robotparser.RobotFileParser(f"{BASE_URL}/robots.txt")
            try:
                status, body = self._get(f"{BASE_URL}/robots.txt")
            except OSError as e:
                raise FetchRefused(f"Could not read ESPNcricinfo's robots.txt ({e}); not fetching anything.") from e
            if status in (401, 403):
                parser.disallow_all = True
                self.refused_reason = f"ESPNcricinfo refused robots.txt (HTTP {status}), so no page may be fetched."
            elif status >= 400:
                parser.allow_all = True  # no robots.txt: nothing is disallowed
            else:
                parser.parse(body.splitlines())
            self._robots = parser
        return self._robots

    def fetch(self, url: str) -> Page | None:
        """The page, from the cache if we have it. None if the page does not exist."""
        page = self.cached(url)
        if page:
            return page
        if self.refused_reason:
            raise FetchRefused(self.refused_reason)
        if not self._robots_parser().can_fetch(self.headers["User-Agent"], url):
            raise FetchRefused(self.refused_reason or f"robots.txt disallows {url}")
        try:
            status, html = self._get(url)
        except OSError as e:
            raise FetchRefused(f"Network error fetching {url}: {e}") from e
        if status in REFUSED_STATUSES:
            self.refused_reason = f"ESPNcricinfo refused the request (HTTP {status}); stopping for this run."
            raise FetchRefused(self.refused_reason)
        if status == 404:
            return None
        if status >= 400:
            raise FetchRefused(f"HTTP {status} for {url}")
        self._save(url, html)
        return Page(url, html, from_cache=False)
