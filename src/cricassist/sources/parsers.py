"""Turn fetched pages and local files into clean text documents."""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from bs4 import BeautifulSoup

MIN_TEXT_CHARS = 200  # shorter pages are navigation or error pages, not articles
MIN_PARAGRAPH_CHARS = 40  # shorter <p> blocks are captions, bylines and buttons

_DROP_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "button", "svg", "iframe"]


@dataclass
class Document:
    url: str
    title: str
    text: str
    kind: str = "other"  # match_report, player_profile or other
    date: str | None = None
    source: str = "ESPNcricinfo"
    ref: str | None = None  # match id or player id, when known

    def to_dict(self) -> dict:
        return asdict(self)


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_page(html: str, url: str, kind: str = "other", ref: str | None = None, source: str = "ESPNcricinfo") -> Document | None:
    """The article in a web page: its title, date and paragraphs. None if the
    page has no article text (an error page, a scorecard with no report)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_DROP_TAGS):
        tag.decompose()

    title = _meta(soup, "og:title", "twitter:title")
    if not title:
        heading = soup.find("h1") or soup.find("title")
        title = _clean(heading.get_text(" ")) if heading else url

    date = _meta(soup, "article:published_time", "og:article:published_time", "date")
    if not date:
        time_tag = soup.find("time")
        date = time_tag.get("datetime") if time_tag and time_tag.get("datetime") else None
    if date:
        date = date[:10]

    container = soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs = [_clean(p.get_text(" ")) for p in container.find_all("p")]
    text = "\n\n".join(p for p in paragraphs if len(p) >= MIN_PARAGRAPH_CHARS)
    if len(text) < MIN_TEXT_CHARS:
        return None
    return Document(url=url, title=_clean(title), text=text, kind=kind, date=date, source=source, ref=ref)


def load_documents_from_dir(directory: Path, source: str = "local") -> list[Document]:
    """Documents from a folder you have the right to use: .html/.htm pages,
    .txt/.md files (title from the first line), or .jsonl files with one
    {"url", "title", "text", "kind", "date"} object per line."""
    docs: list[Document] = []
    for path in sorted(Path(directory).rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in (".html", ".htm"):
            doc = parse_page(path.read_text("utf-8", errors="replace"), url=path.as_uri(), source=source)
            if doc:
                docs.append(doc)
        elif suffix in (".txt", ".md"):
            lines = path.read_text("utf-8", errors="replace").strip().splitlines()
            if lines:
                title = lines[0].lstrip("# ").strip()
                text = "\n".join(lines[1:]).strip()
                if len(text) >= MIN_TEXT_CHARS:
                    docs.append(Document(url=path.as_uri(), title=title, text=text, source=source))
        elif suffix == ".jsonl":
            for line in path.read_text("utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                if d.get("text") and d.get("url"):
                    docs.append(Document(
                        url=d["url"], title=d.get("title") or d["url"], text=d["text"],
                        kind=d.get("kind", "other"), date=d.get("date"),
                        source=d.get("source", source), ref=d.get("ref"),
                    ))
    return docs
