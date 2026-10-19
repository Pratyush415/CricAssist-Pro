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
- [x] Team head-to-heads, venue records
- [x] Phase stats (powerplay, middle overs, death overs)
- [x] Line, length and shot breakdowns from tracking data
- [x] **Done when:** 10 stats checked against ESPNcricinfo all match (`scripts/check_known_stats.py`: 10 of 10; spot-check on Cricinfo pending)

### Mon Oct 19: AI assistant (R1)
- [x] AI model with tool use wrapping the stats queries (Gemini free tier by default; Claude via `CRICASSIST_LLM_PROVIDER=claude`)
- [x] System prompt: no number unless it came from a stats tool; always name the source and filters
- [x] `cricassist chat` command
- [x] Choose the curated scrape list: 30–50 notable matches and ~100 key players, matched to the demo questions (`src/cricassist/sources/scrape_list.json`: 34 matches, 100 players, made by `scripts/make_scrape_list.py`)
- [x] **Done when:** end-to-end chat answers stats questions correctly (the three demo questions so far)

### Tue Oct 20: Cricinfo scraper, vector database and evaluation (R4)
- [x] Check ESPNcricinfo terms of use and `robots.txt`: the terms (Disney terms 2.B(x)) forbid automated collection for AI tools and databases without written permission, and the site refuses automated requests, even robots.txt (HTTP 403)
- [x] Check whether `espncricinfo-python` still works: v1.0.0 works by running a headless browser to bypass the site's bot protection, so it is not used; the project has its own polite fetcher instead
- [x] Scraper (`sources/`) for match reports and player profiles: a few seconds between requests, every page cached locally, honest user agent, reads robots.txt, stops when refused
- [x] Clean text, split into passages, keep source URL, title and date with each passage
- [x] Embed into ChromaDB with a local embedding model (works offline, no extra API key)
- [x] `search_cricinfo_context` tool for the assistant, returning passages with URLs
- [x] `cricassist build-context` command to rebuild the vector database from the cache (or from a folder of documents you may use: `--source-dir`)
- [ ] Test set of 15 journalist questions with verified answers, mixing stats-only, context-only and combined questions
- [ ] **Done when:** at least 13 of 15 correct, no unsourced numbers, context answers cite Cricinfo URLs

### Wed Oct 21: Submission and demo
Submission:
- [ ] Fix remaining issues; all tests pass
- [ ] README: what it does, setup, how to build both databases, how to run, architecture, data sources and licences
- [ ] Merge the work branch into `main` so the submitted repo is complete
- [ ] Fresh-clone check: setup steps work from scratch

Demo:
- [ ] Demo script: 3 rehearsed questions (see Demo questions) showing stats depth, honesty about gaps, and a combined answer, plus backups
- [ ] Both databases pre-built and tested on the demo machine
- [ ] Fallback: screen recording of a full run, in case the network or API fails live
- [ ] Simple Streamlit web page (only if time allows)

## Demo questions
Three questions, one for each thing the project must show. Three keep the demo
within one Gemini model's free daily quota, with room for a rehearsal.

1. How does Kohli fare against left-arm spin in T20s since 2022?
   Stats depth (R1-R3): a matchup by bowling style, filtered by format and date.
2. How long was Dale Steyn ranked number one in Test cricket?
   Honesty: rankings are not in the data, so the assistant says so, gives no
   duration, and shows his Test record by year instead.
3. Tell me about the 2023 World Cup final.
   Combined answer and R4: the full scorecard from the data (dismissals
   included, e.g. "c Head b Maxwell", which the model had wrong from memory),
   and an honest note that no written context is available.

Backups, if asked for more or if one fails:
- How many sixes did Virat Kohli hit at the 2022 T20 World Cup? (8)
- How has Shami been over the years in terms of consistency?
- Who were the best death-over bowlers in IPL 2025, and how does Bumrah compare?
- What's a par first-innings score at the Wankhede in the IPL?

Use the default model (gemini-3.8-flash) for the demo and do not spend its
quota on the day before presenting; run the evaluation on another model.

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
| ~~By Oct 19~~ | ~~AI API key in local `.env`~~ (done: Gemini free-tier key) |
| ~~By Oct 19~~ | ~~Demo questions~~ (done: 3 from Pratyush, 3 chosen to cover the remaining features) |
| By Oct 20 | Interviewer's answer on R4, given ESPNcricinfo's terms (Pratyush is emailing them) |

## Risks
- **Missing matches:** Cricsheet does not have every match (about 160 men's
  Tests since Dec 2001 are missing, mostly before 2009, plus some ODIs and
  T20Is, e.g. Kohli's 122* in the 2022 Asia Cup). Totals can be lower than
  official records. Every stats result reports how many matches it is based
  on and says so; the assistant must pass that on.
- **No Afghanistan internationals:** the Kaggle dataset has none at all, so
  every international against Afghanistan is missing too (e.g. Bumrah has 12
  of his 15 wickets at the 2024 T20 World Cup). Asking about Afghanistan gives
  a clear "not in the dataset" message. Fixable after Oct 21 by loading the
  raw Cricsheet files (already deferred below).
- **Wrong stats:** legal deliveries, retirements and super overs make stats easy
  to get subtly wrong. Mitigated by checking results against ESPNcricinfo.
- **Numbers leaking from scraped text:** the model could quote a figure from a
  retrieved passage. Mitigated by the "numbers only from stats tools" rule and
  checked in the test set.
- **No ESPNcricinfo text:** the pipeline is built and tested, but the live
  context database is empty: the site refuses automated requests and its terms
  forbid scraping for AI tools. The assistant says no written context is
  available and answers from the stats. A source the interviewer approves can
  be loaded with `cricassist build-context --source-dir` without code changes.
- **Scraper breakage or blocking:** Cricinfo may change its pages or block
  requests. Mitigated by caching every page, so the vector database can be
  rebuilt without re-scraping and the demo runs offline.
- **Model arithmetic:** in testing, the model added up rows of a table itself
  and got two counts wrong. The system prompt now forbids totals the tools did
  not compute; the model must re-query with a date range instead.
- **Gemini free-tier quota:** the free tier allows only a few requests per
  minute and a small daily quota per model (used up after ~20 requests on
  Oct 19). The assistant waits out per-minute limits, but on demo day avoid
  heavy testing beforehand, keep `--model` alternatives ready (each model has
  its own quota), or switch to Claude with a paid key.
- **Tight Tuesday:** scraper, vector database and evaluation share one day.
  The curated list is chosen on Monday so Tuesday starts with known targets.
