# CricAssist-Pro Roadmap

Deadline: **Wed Oct 21, 2026** (submission and demo)

## Goal for Oct 21

A command-line chat where a journalist asks questions like *"How has Kohli
done against left-arm spin in T20s since 2022?"* and gets an accurate,
sourced answer: exact numbers from the ball-by-ball database, and written
context from ESPNcricinfo match reports and player profiles.

## Requirements

| # | Requirement | How it is met |
|---|---|---|
| R1 | Statistical queries go to a structured database, never LLM memory or text parsing | Stats tools query DuckDB; the system prompt forbids numbers that did not come from a stats tool |
| R2 | Cricsheet.org ball-by-ball data | The Kaggle dataset is built from Cricsheet (ODC-By licence); attributed in the README |
| R3 | Kaggle "The Ultimate Ball-by-Ball Cricket Dataset" | Loaded into DuckDB (`data/raw/` → `data/processed/`) |
| R4 | ESPNcricinfo scraper (rate-limited) for match reports and player profiles, embedded into a vector database | Polite, cached scraper → ChromaDB with local embeddings → `search_cricinfo_context` tool |

**Rule:** the vector database holds text only. Numbers are never taken from
it; every figure in an answer must come from a stats tool.

## Architecture

```
Journalist question
        │
     Claude (decides which tools to call)
     ├── stats tools ───► DuckDB (ball-by-ball) ───► exact numbers
     └── context tool ──► ChromaDB (Cricinfo text) ─► passages + source URLs
        │
Answer: numbers only from stats tools, context quoted with Cricinfo links
```

## Plan

### Fri Oct 16: Data layer (R2, R3)
- [x] Project structure, packaging, tests
- [x] Dataset `schema.json` and `manifest.json` committed
- [x] DuckDB loader (`stats/db.py`, `stats/ingest.py`)
- [x] Verify CSVs against manifest checksums and schema columns
- [x] Tiny test dataset (2–3 matches) in `tests/`
- [x] `cricassist build-db` command
- [x] **Done when:** full database builds locally and row counts match the manifest

### Sat Oct 17: Stats engine, part 1 (R1) (done early, Oct 16)
- [x] Batting innings and bowling figures views
- [x] Player career and recent form
- [x] Batter vs bowler, batter vs bowling style
- [x] **Done when:** these queries return correct numbers

### Sun Oct 18: Stats engine, part 2 (R1)
- [ ] Team head-to-heads, venue records
- [ ] Phase stats (powerplay, middle overs, death overs)
- [ ] Line, length and shot breakdowns from tracking data
- [ ] **Done when:** 10 stats checked against ESPNcricinfo all match

### Mon Oct 19: AI assistant (R1)
- [ ] Claude API with tool use wrapping the stats queries
- [ ] System prompt: no number unless it came from a stats tool; always name the source and filters
- [ ] `cricassist chat` command
- [ ] Choose the curated scrape list: 30–50 notable matches and ~100 key players, matched to the demo questions
- [ ] **Done when:** end-to-end chat answers stats questions correctly

### Tue Oct 20: Cricinfo scraper, vector database and evaluation (R4)
- [ ] Check ESPNcricinfo terms of use and `robots.txt`
- [ ] Check whether `espncricinfo-python` still works; otherwise a small BeautifulSoup scraper
- [ ] Scraper (`sources/`) for match reports and player profiles: a few seconds between requests, every page cached locally, honest user agent
- [ ] Clean text, split into passages, keep source URL, title and date with each passage
- [ ] Embed into ChromaDB with a local embedding model (works offline, no extra API key)
- [ ] `search_cricinfo_context` tool for the assistant, returning passages with URLs
- [ ] `cricassist build-context` command to rebuild the vector database from the cache
- [ ] Test set of 15 journalist questions with verified answers, mixing stats-only, context-only and combined questions
- [ ] **Done when:** at least 13 of 15 correct, no unsourced numbers, context answers cite Cricinfo URLs

### Wed Oct 21: Submission and demo
Submission:
- [ ] Fix remaining issues; all tests pass
- [ ] README: what it does, setup, how to build both databases, how to run, architecture, data sources and licences
- [ ] Merge the work branch into `main` so the submitted repo is complete
- [ ] Fresh-clone check: setup steps work from scratch

Demo:
- [ ] Demo script: 5–6 rehearsed questions showing stats depth, Cricinfo context with sources, and a combined answer
- [ ] Both databases pre-built and tested on the demo machine
- [ ] Fallback: screen recording of a full run, in case the network or API fails live
- [ ] Simple Streamlit web page (only if time allows)

## Deferred until after Oct 21
- Scraping beyond the curated set (all match reports, news articles)
- Loading raw Cricsheet files for competitions not in the Kaggle dataset
- Full web app
- Automatic dataset refreshes and deployment
- `cricinfo_text_predictions.csv` and the small edge-case files

## Inputs needed
| When | What |
|---|---|
| ~~Oct 16~~ | ~~Small CSVs + 20,000-row samples of the two large CSVs~~ (full dataset provided) |
| ~~Oct 16–17~~ | ~~Run `cricassist build-db` locally on the full dataset~~ (done Oct 16: about a minute, all row counts match) |
| By Oct 19 | Anthropic API key in local `.env` (never committed) |
| By Oct 19 | Demo questions you want to show, to drive the curated scrape list |
| By Oct 20 | Confirmation that scraping fits ESPNcricinfo's terms of use for this submission |

## Risks
- **Missing matches:** Cricsheet does not have every match (about 160 men's
  Tests since Dec 2001 are missing, mostly before 2009, plus some ODIs and
  T20Is, e.g. Kohli's 122* in the 2022 Asia Cup). Totals can be lower than
  official records. Every stats result reports how many matches it is based
  on and says so; the assistant must pass that on.
- **Wrong stats:** legal deliveries, retirements and super overs make stats easy
  to get subtly wrong. Mitigated by checking results against ESPNcricinfo.
- **Numbers leaking from scraped text:** the model could quote a figure from a
  retrieved passage. Mitigated by the "numbers only from stats tools" rule and
  checked in the test set.
- **Scraper breakage or blocking:** Cricinfo may change its pages or block
  requests. Mitigated by caching every page, so the vector database can be
  rebuilt without re-scraping and the demo runs offline.
- **Tight Tuesday:** scraper, vector database and evaluation share one day.
  The curated list is chosen on Monday so Tuesday starts with known targets.
