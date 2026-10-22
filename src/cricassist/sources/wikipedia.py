"""Wikipedia retrieval: articles on matches, tournaments and players for written context.

Uses the MediaWiki Action API the way Wikimedia's API etiquette asks:

- a descriptive User-Agent with a contact URL,
- one request at a time, at least `delay` seconds apart, with maxlag=5 so the
  servers can ask us to back off when they are busy,
- every response cached, so rebuilding the context database makes no requests.

Wikipedia text is licensed CC BY-SA 4.0: passages keep their article URL so
answers can credit it, and the README credits Wikipedia.
"""

import hashlib
import json
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cricassist.sources.cricinfo_client import Opener, urllib_opener

API_URL = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "CricAssistPro/0.1 (cricket research assistant; https://github.com/Pratyush415/CricAssist-Pro)"
DEFAULT_DELAY = 1.0  # seconds between requests
RETRIES = 3

# Sections at the end of an article that are lists of sources, not narrative.
DROP_SECTIONS = {
    "notes", "references", "see also", "external links", "further reading",
    "bibliography", "sources", "citations", "footnotes", "explanatory notes",
}
_HEADING = re.compile(r"^(=+)\s*(.+?)\s*\1\s*$")


class WikipediaError(Exception):
    """Wikipedia could not be reached, or refused the request."""


@dataclass
class Article:
    title: str
    url: str
    text: str


def clean_extract(text: str) -> str:
    """Article text without the trailing reference sections, with headings as plain lines."""
    lines = []
    for line in text.splitlines():
        heading = _HEADING.match(line.strip())
        if heading:
            if heading.group(2).strip().lower() in DROP_SECTIONS:
                break  # everything from here on is notes and references
            lines.append("")
            lines.append(heading.group(2).strip())
            continue
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


class WikipediaClient:
    def __init__(
        self,
        cache_dir: Path,
        delay: float = DEFAULT_DELAY,
        user_agent: str = USER_AGENT,
        opener: Opener = urllib_opener,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        offline: bool = False,
    ):
        self.offline = offline  # cached responses only; never make a request
        self.cache_dir = Path(cache_dir)
        self.delay = delay
        self.headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.opener = opener
        self.sleep = sleep
        self.clock = clock
        self._last_request: float | None = None
        self.requests_made = 0

    def _cache_path(self, params: dict) -> Path:
        key = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        return self.cache_dir / f"{key}.json"

    def _wait_turn(self) -> None:
        if self._last_request is not None:
            wait = self.delay - (self.clock() - self._last_request)
            if wait > 0:
                self.sleep(wait)

    def _get(self, params: dict) -> dict:
        """One API call, from the cache when possible. Retries when Wikipedia asks us to back off."""
        path = self._cache_path(params)
        if path.is_file():
            return json.loads(path.read_text("utf-8"))
        if self.offline:
            raise WikipediaError("Not in the cache, and --no-fetch makes no requests")
        query = {**params, "format": "json", "formatversion": "2", "maxlag": "5"}
        url = f"{API_URL}?{urllib.parse.urlencode(query)}"
        for attempt in range(RETRIES + 1):
            self._wait_turn()
            try:
                status, body = self.opener(url, self.headers)
            except OSError as e:
                raise WikipediaError(f"Could not reach Wikipedia: {e}") from e
            finally:
                self._last_request = self.clock()
                self.requests_made += 1
            data = json.loads(body) if status == 200 and body else {}
            busy = status in (429, 503) or data.get("error", {}).get("code") == "maxlag"
            if busy and attempt < RETRIES:
                self.sleep(5 * (attempt + 1))  # Wikipedia is busy: back off, then try again
                continue
            if status != 200:
                raise WikipediaError(f"Wikipedia returned HTTP {status}")
            if "error" in data:
                raise WikipediaError(f"Wikipedia error: {data['error'].get('info', data['error'])}")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), "utf-8")
            return data
        raise WikipediaError("Wikipedia is busy; try again later")

    def search(self, query: str, limit: int = 3) -> list[str]:
        """Titles of the best-matching articles."""
        data = self._get({"action": "query", "list": "search", "srsearch": query, "srlimit": str(limit)})
        return [r["title"] for r in data.get("query", {}).get("search", [])]

    def categories(self, title: str) -> list[str]:
        """The article's visible categories, e.g. "Category:Indian cricketers"."""
        data = self._get({
            "action": "query", "prop": "categories", "titles": title, "redirects": "1",
            "clshow": "!hidden", "cllimit": "max",
        })
        pages = data.get("query", {}).get("pages", [])
        return [c["title"] for c in pages[0].get("categories", [])] if pages else []

    def article_and_categories(self, title: str) -> tuple[Article | None, list[str]]:
        """The article and its visible categories in one request (used for on-demand lookups)."""
        data = self._get({
            "action": "query", "prop": "extracts|info|categories", "titles": title, "redirects": "1",
            "explaintext": "1", "exsectionformat": "wiki", "inprop": "url", "clshow": "!hidden", "cllimit": "max",
        })
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing") or not pages[0].get("extract"):
            return None, []
        page = pages[0]
        categories = [c["title"] for c in page.get("categories", [])]
        return Article(title=page["title"], url=page["fullurl"], text=clean_extract(page["extract"])), categories

    def article(self, title: str) -> Article | None:
        """The article's plain text (following redirects), or None if there is no such page."""
        data = self._get({
            "action": "query", "prop": "extracts|info", "titles": title, "redirects": "1",
            "explaintext": "1", "exsectionformat": "wiki", "inprop": "url",
        })
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing") or not pages[0].get("extract"):
            return None
        page = pages[0]
        return Article(title=page["title"], url=page["fullurl"], text=clean_extract(page["extract"]))
