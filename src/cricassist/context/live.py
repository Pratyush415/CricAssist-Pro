"""Grow the written context on demand from Wikipedia, for cricket topics only.

The context database starts with the articles from the scrape list. When the
assistant searches it, `LiveWikipedia.ensure()` also searches Wikipedia for the
topic and adds any matching articles not stored yet, so any cricket topic
Wikipedia covers (a series, a rivalry, a ground, an era) can be answered, and
the database grows with use. An article is added only if Wikipedia files it in
a cricket category ("Indian cricketers", "Cricket World Cup finals", ...), so
nothing but cricket gets in. Requests follow the same rules as the scraper:
one a second, cached, with an identifying User-Agent.
"""

import threading

from cricassist.context.store import ContextStore
from cricassist.sources.parsers import Document
from cricassist.sources.wikipedia import WikipediaClient

MAX_NEW_ARTICLES = 3  # per search, to keep a first-time lookup to a few seconds
MAX_CANDIDATES = 6  # search results checked per lookup


def is_cricket(categories: list[str]) -> bool:
    return any("cricket" in c.lower() for c in categories)


def kind_of(categories: list[str]) -> str:
    """match_report, player_profile, tournament or topic, from Wikipedia's categories."""
    text = " ".join(c.lower() for c in categories)
    if "cricketers" in text or "cricket captains" in text:
        return "player_profile"
    if "finals" in text or "cricket matches" in text:
        return "match_report"
    if any(w in text for w in ("seasons", "tournaments", "tours", "series", "cups", "trophy", "leagues")):
        return "tournament"
    return "topic"


class LiveWikipedia:
    def __init__(self, store: ContextStore, client: WikipediaClient, max_new: int = MAX_NEW_ARTICLES):
        self.store = store
        self.client = client
        self.max_new = max_new
        self._lock = threading.Lock()  # web chats can search at the same time

    def candidates(self, query: str) -> list[str]:
        """Search results for the topic as given, then with "cricket" added.
        Wikipedia ranks short topic names well ("Eden Gardens"), so the plain query comes first."""
        titles = []
        for q in (query, f"{query} cricket"):
            for title in self.client.search(q, limit=5):
                if title not in titles:
                    titles.append(title)
        return titles[:MAX_CANDIDATES]

    def ensure(self, query: str) -> list[str]:
        """Add Wikipedia's best cricket articles for the query that are not stored yet.
        Returns the titles added."""
        added = []
        with self._lock:
            for title in self.candidates(query):
                if len(added) >= self.max_new:
                    break
                if self.store.has_title(title):
                    continue
                article, categories = self.client.article_and_categories(title)
                if article is None or not is_cricket(categories):
                    continue  # no such page, or not a cricket article: leave it out
                if self.store.has_title(article.title):  # a redirect to an article already stored
                    continue
                self.store.add([Document(url=article.url, title=article.title, text=article.text,
                                         kind=kind_of(categories), source="Wikipedia")])
                added.append(article.title)
        return added
