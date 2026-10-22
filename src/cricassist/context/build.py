"""Build the context vector database from Wikipedia, ESPNcricinfo or local documents."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from cricassist.context.store import ContextStore
from cricassist.sources.cricinfo_client import CricinfoClient, FetchRefused, match_report_url, player_profile_url
from cricassist.sources.parsers import Document, load_documents_from_dir, parse_page
from cricassist.sources.wikipedia import Article, WikipediaClient, WikipediaError


@dataclass
class ContextReport:
    pages_listed: int = 0
    fetched: int = 0
    from_cache: int = 0
    not_found: int = 0
    no_article_text: int = 0
    refused: int = 0
    refused_reason: str | None = None
    local_documents: int = 0
    documents: int = 0
    passages: int = 0
    total_passages: int = 0
    errors: list[str] = field(default_factory=list)


def load_scrape_list() -> dict:
    """The packaged list of matches and players to collect (sources/scrape_list.json)."""
    text = resources.files("cricassist").joinpath("sources", "scrape_list.json").read_text("utf-8")
    return json.loads(text)


def scrape_targets(scrape_list: dict) -> list[tuple[str, str, str]]:
    """(url, kind, ref) for every match report and player profile in the list."""
    targets = [(match_report_url(m["match_id"]), "match_report", str(m["match_id"])) for m in scrape_list["matches"]]
    targets += [
        (player_profile_url(p["cricinfo_id"], p["name"]), "player_profile", str(p["cricinfo_id"]))
        for p in scrape_list["players"]
    ]
    return targets


def build_context(
    store: ContextStore,
    client: CricinfoClient | None = None,
    scrape_list: dict | None = None,
    source_dir: Path | None = None,
    fetch: bool = True,
    limit: int | None = None,
    log: Callable[[str], None] = print,
) -> ContextReport:
    """Collect documents and add their passages to the store.

    Pages on the scrape list come from the client's cache, or are fetched when
    `fetch` is on. Once the site refuses a request, the rest of the list is
    taken from the cache only. `source_dir` adds documents you have the right
    to use from a local folder.
    """
    report = ContextReport()
    docs: list[Document] = []

    if source_dir:
        local = load_documents_from_dir(Path(source_dir))
        report.local_documents = len(local)
        docs += local
        log(f"{len(local)} documents from {source_dir}")

    if client is not None and scrape_list is not None:
        targets = scrape_targets(scrape_list)[:limit]
        report.pages_listed = len(targets)
        log(f"{len(targets)} pages on the scrape list" + ("" if fetch else " (cache only)"))
        for url, kind, ref in targets:
            page = client.cached(url)
            if page is None and fetch and not client.refused_reason:
                try:
                    page = client.fetch(url)
                    if page is None:
                        report.not_found += 1
                        continue
                except FetchRefused as e:
                    report.refused_reason = str(e)
                    log(f"Stopped fetching: {e}")
            if page is None:
                report.refused += 1 if (fetch and client.refused_reason) else 0
                continue
            if page.from_cache:
                report.from_cache += 1
            else:
                report.fetched += 1
            doc = parse_page(page.html, url, kind=kind, ref=ref)
            if doc is None:
                report.no_article_text += 1
                continue
            docs.append(doc)
        report.refused_reason = report.refused_reason or client.refused_reason

    report.documents = len(docs)
    report.passages = store.add(docs) if docs else 0
    report.total_passages = store.count()
    return report


# --- Wikipedia ---------------------------------------------------------------


@dataclass
class WikiTarget:
    query: str  # what to search Wikipedia for
    kind: str  # match_report, tournament or player_profile
    ref: str  # match id or player id
    title_must_include: list[str]  # words the article title must contain (a year, a surname)
    fallback_queries: list[str] = field(default_factory=list)  # tried in order if the query finds nothing


def known_as(name: str, full_name: str | None) -> str:
    """The name a player is usually known by: "S Mandhana" + "Smriti Shriniwas Mandhana" -> "Smriti Mandhana"."""
    tokens = name.split()
    initials = tokens[0] if len(tokens) > 1 and tokens[0].isupper() and len(tokens[0]) <= 3 else None
    if not initials or not full_name:
        return name
    given = next((t for t in full_name.split() if t[0].upper() == initials[0]), None)
    return f"{given} {tokens[-1]}" if given else name


def wikipedia_targets(scrape_list: dict) -> list[WikiTarget]:
    """What to look up on Wikipedia for each match and player on the scrape list:
    the final's own article for finals, the tournament or series article for other
    matches, and each player's biography."""
    targets, seen = [], set()
    for m in scrape_list["matches"]:
        year = m["date"][:4]
        if "final" in m.get("reason", "").lower():
            target = WikiTarget(f"{year} {m['event']} final", "match_report", str(m["match_id"]), [year])
        else:
            target = WikiTarget(f"{year} {m['event']}", "tournament", str(m["match_id"]), [year])
        if target.query not in seen:
            seen.add(target.query)
            targets.append(target)
    for p in scrape_list["players"]:
        name = p.get("full_name") or p["name"]
        surname = p["name"].split()[-1]
        # Full legal names do not always match Wikipedia's title, so also try the usual name.
        usual = known_as(p["name"], p.get("full_name"))
        fallbacks = [f"{usual} cricketer"] if usual != name else []
        targets.append(WikiTarget(f"{name} cricketer", "player_profile", p["player_id"], [surname], fallbacks))
    return targets


def find_article(client: WikipediaClient, target: WikiTarget) -> Article | None:
    """The first search result whose title fits the target and whose text is about cricket.
    For a final, an article about the final itself beats the tournament's article."""
    for query in [target.query, *target.fallback_queries]:
        titles = client.search(query, limit=5)
        if target.kind == "match_report":
            titles.sort(key=lambda t: "final" not in t.lower())  # stable: keeps search order otherwise
        for title in titles:
            if not all(word.lower() in title.lower() for word in target.title_must_include):
                continue
            article = client.article(title)
            if article and "cricket" in article.text[:3000].lower():
                return article
    return None


def build_wikipedia_context(
    store: ContextStore,
    client: WikipediaClient,
    scrape_list: dict,
    limit: int | None = None,
    log: Callable[[str], None] = print,
) -> ContextReport:
    """Look up each target on Wikipedia and add the articles' passages to the store."""
    report = ContextReport()
    targets = wikipedia_targets(scrape_list)[:limit]
    report.pages_listed = len(targets)
    log(f"Looking up {len(targets)} matches, tournaments and players on Wikipedia...")
    docs: dict[str, Document] = {}
    requests_before = client.requests_made
    for i, target in enumerate(targets, 1):
        try:
            article = find_article(client, target)
        except WikipediaError as e:
            report.refused_reason = str(e)
            log(f"Stopped: {e}")
            break
        if article is None:
            report.not_found += 1
            report.errors.append(f"no article found for {target.query!r}")
            continue
        if article.url not in docs:  # several targets can lead to the same article
            docs[article.url] = Document(url=article.url, title=article.title, text=article.text,
                                         kind=target.kind, source="Wikipedia", ref=target.ref)
        if i % 20 == 0:
            log(f"  {i} of {len(targets)} looked up")
    report.fetched = client.requests_made - requests_before
    report.documents = len(docs)
    report.passages = store.add(list(docs.values())) if docs else 0
    report.total_passages = store.count()
    return report
