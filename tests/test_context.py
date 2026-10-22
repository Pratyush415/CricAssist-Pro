"""The context pipeline: fetching, parsing, passages, the vector store and its tool.

Nothing here touches the network: the fetcher gets a fake opener and the
vector store a fake embedding function. Fixture pages are written for these
tests (a made-up match), not copied from any site.
"""

import json
from pathlib import Path

import pytest
from chromadb import Documents, EmbeddingFunction, Embeddings

from cricassist.assistant import tools
from cricassist.assistant.tools import run_tool
from cricassist.context import build as build_module
from cricassist.context.build import build_context, load_scrape_list, scrape_targets
from cricassist.context.passages import split_passages
from cricassist.context.store import ContextStore
from cricassist.sources.cricinfo_client import (
    BASE_URL,
    CricinfoClient,
    FetchRefused,
    match_report_url,
    player_profile_url,
)
from cricassist.sources.parsers import Document, load_documents_from_dir, parse_page

FIXTURES = Path(__file__).parent / "context_fixtures"
REPORT_HTML = (FIXTURES / "match_report.html").read_text("utf-8")
PROFILE_HTML = (FIXTURES / "player_profile.html").read_text("utf-8")


class WordEmbedding(EmbeddingFunction):
    """A tiny bag-of-words embedding: enough for 'closest passage' tests, no model download."""

    VOCAB = ["swing", "bowler", "captain", "chase", "final", "spin", "footwork", "academy", "partnership", "coach"]

    def __init__(self):
        pass

    def __call__(self, input: Documents) -> Embeddings:
        return [[float(text.lower().count(w)) + 0.01 for w in self.VOCAB] for text in input]

    @staticmethod
    def name() -> str:
        return "test-words"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config):
        return WordEmbedding()


@pytest.fixture
def store(tmp_path):
    return ContextStore(tmp_path / "context", embedding_function=WordEmbedding())


# --- Fetcher -----------------------------------------------------------------


class FakeSite:
    """Serves canned responses by URL and records every request."""

    def __init__(self, pages: dict[str, tuple[int, str]]):
        self.pages = pages
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        self.requests.append((url, headers))
        return self.pages.get(url, (404, ""))


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


ROBOTS_OK = (200, "User-agent: *\nDisallow: /private/\n")
URL = match_report_url(1384439)


def client_for(tmp_path, site, clock=None, delay=5.0):
    clock = clock or FakeClock()
    return CricinfoClient(tmp_path / "cache", delay=delay, opener=site, sleep=clock.sleep, clock=clock.clock)


def test_fetch_caches_pages_and_sends_an_honest_user_agent(tmp_path):
    site = FakeSite({f"{BASE_URL}/robots.txt": ROBOTS_OK, URL: (200, REPORT_HTML)})
    client = client_for(tmp_path, site)
    first = client.fetch(URL)
    again = client.fetch(URL)
    assert not first.from_cache and again.from_cache and again.html == REPORT_HTML
    assert [u for u, _ in site.requests] == [f"{BASE_URL}/robots.txt", URL]  # page fetched once
    assert "CricAssistPro" in site.requests[1][1]["User-Agent"]
    assert "Mozilla" not in site.requests[1][1]["User-Agent"]


def test_fetch_waits_between_requests(tmp_path):
    other = match_report_url(1)
    site = FakeSite({f"{BASE_URL}/robots.txt": ROBOTS_OK, URL: (200, REPORT_HTML), other: (200, REPORT_HTML)})
    clock = FakeClock()
    client = client_for(tmp_path, site, clock, delay=5.0)
    client.fetch(URL)
    client.fetch(other)
    assert clock.slept == [5.0, 5.0]  # before the page after robots.txt, and before the next page


def test_robots_txt_refused_means_nothing_is_fetched(tmp_path):
    site = FakeSite({f"{BASE_URL}/robots.txt": (403, "Access Denied")})
    client = client_for(tmp_path, site)
    with pytest.raises(FetchRefused, match="robots.txt"):
        client.fetch(URL)
    assert [u for u, _ in site.requests] == [f"{BASE_URL}/robots.txt"]


def test_robots_txt_disallowed_path_is_skipped(tmp_path):
    private = f"{BASE_URL}/private/page"
    site = FakeSite({f"{BASE_URL}/robots.txt": ROBOTS_OK, private: (200, REPORT_HTML)})
    with pytest.raises(FetchRefused, match="disallows"):
        client_for(tmp_path, site).fetch(private)
    assert private not in [u for u, _ in site.requests]


def test_a_refusal_stops_the_run(tmp_path):
    other = match_report_url(1)
    site = FakeSite({f"{BASE_URL}/robots.txt": ROBOTS_OK, URL: (403, ""), other: (200, REPORT_HTML)})
    client = client_for(tmp_path, site)
    with pytest.raises(FetchRefused, match="HTTP 403"):
        client.fetch(URL)
    with pytest.raises(FetchRefused):
        client.fetch(other)
    assert other not in [u for u, _ in site.requests]  # no more requests after a refusal


def test_missing_page_is_none(tmp_path):
    site = FakeSite({f"{BASE_URL}/robots.txt": ROBOTS_OK})
    assert client_for(tmp_path, site).fetch(URL) is None


def test_urls():
    assert match_report_url(1384439) == f"{BASE_URL}/ci/engine/match/1384439.html"
    assert player_profile_url(253802, "V Kohli") == f"{BASE_URL}/cricketers/v-kohli-253802"
    assert player_profile_url(1, "Mohammed Shami") .endswith("/mohammed-shami-1")


# --- Parsing -----------------------------------------------------------------


def test_parse_match_report():
    doc = parse_page(REPORT_HTML, URL, kind="match_report", ref="1384439")
    assert doc.title == "Patel's late burst seals the final for Northshire"
    assert doc.date == "2024-03-10"
    assert doc.text.startswith("Northshire won a tense final")
    assert len(doc.text.split("\n\n")) == 4
    for junk in ("window.tracking", "Sign in", "Terms of use", "Related:", "By a test author"):
        assert junk not in doc.text


def test_parse_profile_uses_fallbacks():
    doc = parse_page(PROFILE_HTML, "https://example.org/p", kind="player_profile")
    assert doc.title == "Arjun Patel: player profile"  # twitter:title
    assert doc.date == "2025-01-02"  # <time datetime>
    assert "reverse swing" in doc.text


def test_pages_without_article_text_are_skipped():
    assert parse_page("<html><body><p>Access Denied</p></body></html>", URL) is None


def test_load_documents_from_dir():
    docs = load_documents_from_dir(FIXTURES / "local")
    titles = sorted(d.title for d in docs)
    assert titles == ["Southvale season notes", "Southvale through to the final"]
    report = next(d for d in docs if d.kind == "match_report")
    assert report.url == "https://example.org/reports/semi-final" and report.date == "2024-03-03"


# --- Passages ----------------------------------------------------------------


def test_split_passages_keeps_paragraphs_and_overlaps():
    paragraphs = [" ".join(f"p{i}w{j}" for j in range(60)) for i in range(5)]
    passages = split_passages("\n\n".join(paragraphs), max_words=150, overlap=20)
    assert all(len(p.split()) <= 150 + 20 for p in passages)
    assert passages[1].split()[:20] == passages[0].split()[-20:]  # overlap
    all_words = {w for p in passages for w in p.split()}
    assert all_words == {w for para in paragraphs for w in para.split()}  # nothing lost


def test_split_passages_breaks_long_paragraphs():
    long_para = " ".join(f"Sentence number {i} is here." for i in range(100))
    passages = split_passages(long_para, max_words=50, overlap=0)
    assert len(passages) > 5 and all(len(p.split()) <= 50 for p in passages)


def test_short_text_is_one_passage():
    assert split_passages("One short paragraph.") == ["One short paragraph."]


# --- Store -------------------------------------------------------------------


def test_store_add_and_search(store):
    report = parse_page(REPORT_HTML, URL, kind="match_report", ref="1384439")
    profile = parse_page(PROFILE_HTML, "https://example.org/p", kind="player_profile")
    assert store.add([report, profile]) == store.count() > 0
    top = store.search("which bowler made the ball swing late", n=1)[0]
    assert top["url"] in (URL, "https://example.org/p") and "swing" in top["text"]
    assert all(h["kind"] == "player_profile" for h in store.search("bowler swing", n=3, kind="player_profile"))


def test_re_adding_a_document_replaces_its_passages(store):
    doc = parse_page(REPORT_HTML, URL, kind="match_report")
    longer = Document(url=URL, title=doc.title, text="\n\n".join([doc.text] * 3), kind="match_report")
    store.add([longer])
    first = store.count()
    store.add([doc])
    assert store.count() == 1 < first


def test_empty_store_search(store):
    assert store.search("anything") == []


# --- Build -------------------------------------------------------------------


def test_scrape_list_targets():
    scrape_list = load_scrape_list()
    targets = scrape_targets(scrape_list)
    assert len(targets) == len(scrape_list["matches"]) + len(scrape_list["players"])
    assert (match_report_url(1384439), "match_report", "1384439") in targets


def test_build_context_from_site_cache_and_folder(tmp_path, store):
    small_list = {"matches": [{"match_id": 1384439}, {"match_id": 1}], "players": [{"cricinfo_id": 7, "name": "A Patel"}]}
    site = FakeSite({
        f"{BASE_URL}/robots.txt": ROBOTS_OK,
        match_report_url(1384439): (200, REPORT_HTML),
        player_profile_url(7, "A Patel"): (200, PROFILE_HTML),
    })
    client = client_for(tmp_path, site)
    report = build_context(store, client, small_list, source_dir=FIXTURES / "local", log=lambda m: None)
    assert (report.fetched, report.not_found, report.local_documents, report.documents) == (2, 1, 2, 4)
    assert report.passages == store.count() > 0

    # A rebuild with no network uses the cache.
    offline = client_for(tmp_path, FakeSite({}))
    again = build_context(store, offline, small_list, fetch=False, log=lambda m: None)
    assert (again.from_cache, again.fetched) == (2, 0)


def test_build_context_when_the_site_refuses(tmp_path, store):
    site = FakeSite({f"{BASE_URL}/robots.txt": (403, "Access Denied")})
    report = build_context(store, client_for(tmp_path, site), load_scrape_list(), log=lambda m: None)
    assert report.fetched == 0 and report.documents == 0 and "robots.txt" in report.refused_reason
    assert len(site.requests) == 1  # only robots.txt was ever asked for


# --- Assistant tool ----------------------------------------------------------


def test_context_tool_when_empty(db, store):
    tools.set_context_store(store)
    try:
        result, is_error = run_tool(db, "search_written_context", {"query": "final"})
        result = json.loads(result)
        assert not is_error and result["passages"] == [] and "No written context" in result["note"]
    finally:
        tools.set_context_store(None)


def test_context_tool_returns_passages_with_urls(db, store):
    store.add([parse_page(REPORT_HTML, URL, kind="match_report")])
    tools.set_context_store(store)
    try:
        result = json.loads(run_tool(db, "search_written_context", {"query": "captain chase final", "n": 2})[0])
        assert result["passages"][0]["url"] == URL
        assert "never take numbers" in result["note"] and "Wikipedia" in result["note"]
    finally:
        tools.set_context_store(None)


def test_build_context_cli(tmp_path, monkeypatch, capsys):
    from cricassist.context import store as store_module
    from cricassist.main import main

    monkeypatch.setenv("CRICASSIST_CONTEXT_PATH", str(tmp_path / "ctx"))
    monkeypatch.setattr(store_module, "default_embedding_function", WordEmbedding)
    assert main(["build-context", "--local-only", "--source-dir", str(FIXTURES / "local")]) == 0
    assert "from 2 local documents" in capsys.readouterr().out
    assert build_module  # imported for monkeypatch targets


# --- Wikipedia ---------------------------------------------------------------

from urllib.parse import parse_qs, urlparse  # noqa: E402

from cricassist.context.build import (  # noqa: E402
    WikiTarget,
    build_wikipedia_context,
    find_article,
    known_as,
    wikipedia_targets,
)
from cricassist.sources.wikipedia import WikipediaClient, WikipediaError, clean_extract  # noqa: E402

ARTICLES = {
    "2023 Cricket World Cup final": "The final was a One Day International cricket match played in Ahmedabad.\n\n"
                                    "== Match ==\nAustralia chose to bowl and Travis Head scored a century in the chase.\n\n"
                                    "== References ==\n1. A source that must not be kept.",
    "2023 Cricket World Cup": "The 2023 Cricket World Cup was the 13th edition of the tournament.",
    "Jasprit Bumrah": "Jasprit Bumrah is an Indian international cricketer known for his unusual bowling action.",
    "Jasprit (film)": "Jasprit is a 2019 film about a cook.",
}
CATEGORIES = {
    "2023 Cricket World Cup final": ["Category:Cricket World Cup finals"],
    "2023 Cricket World Cup": ["Category:2023 Cricket World Cup"],
    "Jasprit Bumrah": ["Category:Indian cricketers"],
    "Jasprit (film)": ["Category:2019 films"],
}
SEARCHES = {
    "2023 ICC Cricket World Cup final": ["2023 Cricket World Cup", "2023 Cricket World Cup final"],
    "Jasprit Jasbirsingh Bumrah cricketer": ["Jasprit (film)", "Jasprit Bumrah"],
}


class FakeWikipedia:
    """Answers the MediaWiki API from the dictionaries above and records each request."""

    def __init__(self, status=200):
        self.status = status
        self.requests = []

    def __call__(self, url, headers):
        self.requests.append((url, headers))
        if self.status != 200:
            return self.status, ""
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if q.get("list") == "search":
            hits = SEARCHES.get(q["srsearch"], [])
            return 200, json.dumps({"query": {"search": [{"title": t} for t in hits]}})
        title = q["titles"]
        if title not in ARTICLES:
            return 200, json.dumps({"query": {"pages": [{"title": title, "missing": True}]}})
        page = {"title": title, "fullurl": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")}
        props = q.get("prop", "")
        if "extracts" in props:
            page["extract"] = ARTICLES[title]
        if "categories" in props:
            page["categories"] = [{"title": c} for c in CATEGORIES.get(title, [])]
        return 200, json.dumps({"query": {"pages": [page]}})


def wiki_client(tmp_path, site, clock=None, **kwargs):
    clock = clock or FakeClock()
    return WikipediaClient(tmp_path / "wiki", opener=site, sleep=clock.sleep, clock=clock.clock, **kwargs)


def test_clean_extract_drops_reference_sections():
    text = clean_extract(ARTICLES["2023 Cricket World Cup final"])
    assert "Match" in text and "Travis Head" in text
    assert "References" not in text and "must not be kept" not in text and "==" not in text


def test_wikipedia_client_identifies_itself_waits_and_caches(tmp_path):
    site, clock = FakeWikipedia(), FakeClock()
    client = wiki_client(tmp_path, site, clock, delay=1.0)
    assert client.search("2023 ICC Cricket World Cup final")[1] == "2023 Cricket World Cup final"
    article = client.article("2023 Cricket World Cup final")
    assert article.url == "https://en.wikipedia.org/wiki/2023_Cricket_World_Cup_final"
    assert client.article("2023 Cricket World Cup final").title == article.title  # from the cache
    assert len(site.requests) == 2 and clock.slept == [1.0]
    url, headers = site.requests[0]
    assert "CricAssistPro" in headers["User-Agent"] and "github.com" in headers["User-Agent"]
    assert "maxlag=5" in url
    assert client.article("No such page") is None


def test_wikipedia_client_backs_off_then_gives_up(tmp_path):
    site, clock = FakeWikipedia(status=429), FakeClock()
    with pytest.raises(WikipediaError):
        wiki_client(tmp_path, site, clock).search("anything")
    assert len(site.requests) == 4 and [s for s in clock.slept if s >= 5] == [5, 10, 15]


def test_wikipedia_offline_uses_only_the_cache(tmp_path):
    online = wiki_client(tmp_path, FakeWikipedia())
    online.search("Jasprit Jasbirsingh Bumrah cricketer")
    offline_site = FakeWikipedia()
    offline = wiki_client(tmp_path, offline_site, offline=True)
    assert offline.search("Jasprit Jasbirsingh Bumrah cricketer") == ["Jasprit (film)", "Jasprit Bumrah"]
    with pytest.raises(WikipediaError, match="no-fetch"):
        offline.article("Jasprit Bumrah")
    assert offline_site.requests == []


def test_wikipedia_targets():
    targets = wikipedia_targets({
        "matches": [
            {"match_id": 1, "date": "2023-11-19", "event": "ICC Cricket World Cup", "reason": "Final (male), since 2019"},
            {"match_id": 2, "date": "2022-10-23", "event": "ICC Men's T20 World Cup", "reason": "Demo Q3"},
            {"match_id": 3, "date": "2022-10-27", "event": "ICC Men's T20 World Cup", "reason": "Demo Q3"},
        ],
        "players": [{"player_id": "p1", "name": "JJ Bumrah", "full_name": "Jasprit Jasbirsingh Bumrah"}],
    })
    assert [(t.query, t.kind) for t in targets] == [
        ("2023 ICC Cricket World Cup final", "match_report"),
        ("2022 ICC Men's T20 World Cup", "tournament"),  # both 2022 matches share the tournament article
        ("Jasprit Jasbirsingh Bumrah cricketer", "player_profile"),
    ]
    assert targets[2].title_must_include == ["Bumrah"]


def test_find_article_prefers_the_final_and_checks_the_subject(tmp_path):
    client = wiki_client(tmp_path, FakeWikipedia())
    final = find_article(client, WikiTarget("2023 ICC Cricket World Cup final", "match_report", "1", ["2023"]))
    assert final.title == "2023 Cricket World Cup final"  # not the tournament, though search ranked it first
    player = find_article(client, WikiTarget("Jasprit Jasbirsingh Bumrah cricketer", "player_profile", "p1", ["Bumrah"]))
    assert player.title == "Jasprit Bumrah"  # the film's title lacks the surname
    assert find_article(client, WikiTarget("Nobody cricketer", "player_profile", "x", ["Nobody"])) is None


def test_build_wikipedia_context(tmp_path, store):
    scrape_list = {
        "matches": [{"match_id": 1, "date": "2023-11-19", "event": "ICC Cricket World Cup", "reason": "Final"}],
        "players": [
            {"player_id": "p1", "name": "JJ Bumrah", "full_name": "Jasprit Jasbirsingh Bumrah"},
            {"player_id": "p2", "name": "A Nobody", "full_name": "Arthur Nobody"},
        ],
    }
    report = build_wikipedia_context(store, wiki_client(tmp_path, FakeWikipedia()), scrape_list, log=lambda m: None)
    assert (report.pages_listed, report.documents, report.not_found) == (3, 2, 1)
    hits = store.search("bowling action", n=3)
    assert {h["source"] for h in hits} == {"Wikipedia"}
    assert any(h["url"].endswith("/Jasprit_Bumrah") for h in hits)


def test_known_as():
    assert known_as("S Mandhana", "Smriti Shriniwas Mandhana") == "Smriti Mandhana"
    assert known_as("C Athapaththu", "Athapaththu Mudiyanselage Chamari Jayangani Kumai Athapaththu") == "Chamari Athapaththu"
    assert known_as("Junaid Siddique", "Muhammad Junaid Siddique") == "Junaid Siddique"  # no initials: use as is
    assert known_as("V Kohli", None) == "V Kohli"


def test_find_article_falls_back_to_the_usual_name(tmp_path):
    SEARCHES["Jasprit Bumrah cricketer"] = ["Jasprit Bumrah"]
    try:
        target = WikiTarget("Jasprit Jasbirsingh Unknown cricketer", "player_profile", "p1", ["Bumrah"],
                            ["Jasprit Bumrah cricketer"])
        assert find_article(wiki_client(tmp_path, FakeWikipedia()), target).title == "Jasprit Bumrah"
    finally:
        del SEARCHES["Jasprit Bumrah cricketer"]



# --- On-demand Wikipedia lookups ---------------------------------------------

from cricassist.context.live import LiveWikipedia, is_cricket, kind_of  # noqa: E402


def test_is_cricket_and_kind_of():
    assert is_cricket(["Category:Indian cricketers"]) and not is_cricket(["Category:2019 films"])
    assert kind_of(["Category:Indian cricketers"]) == "player_profile"
    assert kind_of(["Category:Cricket World Cup finals"]) == "match_report"
    assert kind_of(["Category:Border–Gavaskar Trophy series", "Category:Cricket"]) == "tournament"
    assert kind_of(["Category:Cricket grounds in West Bengal"]) == "topic"


def test_live_lookup_adds_only_new_cricket_articles(tmp_path, store):
    SEARCHES["Jasprit"] = ["Jasprit (film)", "Jasprit Bumrah"]
    try:
        site = FakeWikipedia()
        live = LiveWikipedia(store, wiki_client(tmp_path, site))
        assert live.ensure("Jasprit") == ["Jasprit Bumrah"]  # the film is not cricket: left out
        assert store.has_title("Jasprit Bumrah") and not store.has_title("Jasprit (film)")
        searches = [u for u, _ in site.requests if "list=search" in u]
        assert "srsearch=Jasprit&" in searches[0]  # the plain topic is searched first
        before = len(site.requests)
        assert live.ensure("Jasprit") == []  # already stored, and every response is cached
        assert len(site.requests) == before
    finally:
        del SEARCHES["Jasprit"]


def test_live_lookup_stops_at_the_limit(tmp_path, store):
    SEARCHES["World Cup"] = ["2023 Cricket World Cup final", "2023 Cricket World Cup", "Jasprit Bumrah"]
    try:
        live = LiveWikipedia(store, wiki_client(tmp_path, FakeWikipedia()), max_new=2)
        assert live.ensure("World Cup") == ["2023 Cricket World Cup final", "2023 Cricket World Cup"]
    finally:
        del SEARCHES["World Cup"]


def test_context_tool_looks_up_wikipedia(db, store, tmp_path):
    SEARCHES["Jasprit"] = ["Jasprit Bumrah"]
    tools.set_context_store(store, live=LiveWikipedia(store, wiki_client(tmp_path, FakeWikipedia())))
    try:
        result = json.loads(run_tool(db, "search_written_context", {"query": "Jasprit"})[0])
        assert result["added_from_wikipedia"] == ["Jasprit Bumrah"]
        assert result["passages"][0]["url"].endswith("/Jasprit_Bumrah")
    finally:
        tools.set_context_store(None)
        del SEARCHES["Jasprit"]


def test_context_tool_works_when_wikipedia_is_unreachable(db, store, tmp_path):
    store.add([parse_page(REPORT_HTML, URL, kind="match_report")])
    tools.set_context_store(store, live=LiveWikipedia(store, wiki_client(tmp_path, FakeWikipedia(status=503))))
    try:
        result = json.loads(run_tool(db, "search_written_context", {"query": "captain chase final"})[0])
        assert "Could not reach Wikipedia" in result["live_lookup"]
        assert result["passages"][0]["url"] == URL  # the stored passages still answer
    finally:
        tools.set_context_store(None)
