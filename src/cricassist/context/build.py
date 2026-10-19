"""Build the context vector database from the scrape list and local documents."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from cricassist.context.store import ContextStore
from cricassist.sources.cricinfo_client import CricinfoClient, FetchRefused, match_report_url, player_profile_url
from cricassist.sources.parsers import Document, load_documents_from_dir, parse_page


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
