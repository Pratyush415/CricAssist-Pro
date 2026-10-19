# CricAssist-Pro
AI co-pilot for sports journalists that retrieves real-time match data, historical stats, and editorial context exclusively from ESPNcricinfo, combined with an offline ball-by-ball statistical database.

## Project structure

```
CricAssist-Pro/
├── src/cricassist/
│   ├── main.py            # CLI entry point (`cricassist`)
│   ├── config.py          # settings from environment / .env
│   ├── sources/           # ESPNcricinfo retrieval
│   │   ├── cricinfo_client.py
│   │   └── parsers.py
│   ├── stats/             # offline ball-by-ball database
│   │   ├── dataset/       # schema.json + manifest.json for the CSV dataset
│   │   ├── models.py
│   │   ├── db.py
│   │   ├── ingest.py
│   │   └── queries.py
│   └── assistant/         # AI co-pilot
│       ├── agent.py
│       ├── tools.py
│       └── prompts.py
├── data/
│   ├── raw/               # downloaded ball-by-ball files (not committed)
│   └── processed/         # built database (not committed)
├── scripts/               # one-off data jobs
├── tests/
├── pyproject.toml
└── .env.example
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pytest
```

## Asking questions

1. Put an API key in `.env` (copy `.env.example`). The default provider is
   Gemini, which has a free tier: get a key at aistudio.google.com. To use
   Claude instead, set `CRICASSIST_LLM_PROVIDER=claude` and `ANTHROPIC_API_KEY`.
2. Build the stats database (below), then run:

   ```bash
   cricassist chat                                   # a conversation
   cricassist chat "How many sixes did Kohli hit at the 2022 T20 World Cup?"
   cricassist chat --model gemini-3.5-flash "..."    # another model
   ```

The assistant answers by calling the stats tools in `assistant/tools.py` and
prints each call, so you can see where every number came from. Its rules
(`assistant/prompts.py`): every number must come from a tool result, never from
the model's memory or its own arithmetic across rows; when the data cannot
answer (for example ICC rankings), it says so and offers what the data can show.

## Web app

```bash
cricassist web          # opens http://127.0.0.1:8000 in your browser
cricassist web --port 8080 --no-browser
```

A local chat interface over the same assistant (`src/cricassist/web/`): a
splash screen with the CricAssist logo (the i's dotted with red leather balls,
the final t drawn as a bat), a ChatGPT-style thread, a side panel of chat
sessions, and a model picker. While the assistant works, each stats tool it
calls appears live, and the answer types itself out as the model writes it
(streamed); each answer can be expanded to show the tools behind it.
Chats are saved to `data/processed/chats.json` after every change and come
back when the server restarts; continuing an old chat gives the model its
earlier questions and answers, so follow-ups still make sense. Switching the
model mid-chat starts a fresh conversation with the new model. It listens on
this computer only.

## Evaluation

`cricassist eval` asks the assistant 15 journalist questions
(`evaluation/questions.json`): 10 stats questions, 2 the data cannot answer
(ICC rankings, Afghanistan internationals), 2 that need written context, and 1
combined. Expected answers were checked against the database. Each answer is
graded on two things:

- **Correct**: it contains the expected facts, and for questions the data
  cannot answer it says so without inventing anything (no ranking durations,
  no made-up quotes).
- **Sourced**: every number in it appears in the question or in a tool call or
  result. Any other number could only have come from the model's memory or its
  own arithmetic, and is reported.

```bash
cricassist eval                          # run all 15 (resumes an earlier run)
cricassist eval --model gemini-3.5-flash # another model, with its own free quota
cricassist eval --only steyn_ranking     # one question
cricassist eval --summary                # print the last results
```

Latest results: **14 of 15 correct, 15 of 15 fully sourced** (`docs/evaluation.md`).
Results, with every answer and tool call, go to `data/processed/eval/`. A run
takes about 45 model requests, more than one Gemini model's free daily quota,
so it saves after each question and picks up where it stopped.

## Written context (R4)

`cricassist build-context` builds a ChromaDB vector database of match reports
and player profiles for the assistant's `search_cricinfo_context` tool:

1. Fetch the pages on `sources/scrape_list.json` (34 matches, 100 players)
   with a polite client (`sources/cricinfo_client.py`): it reads robots.txt,
   waits at least 3 seconds between requests, identifies itself honestly,
   caches every page in `data/raw/cricinfo_cache/`, and stops when refused.
2. Extract the article text (`sources/parsers.py`), split it into overlapping
   passages that keep their URL, title and date (`context/passages.py`).
3. Embed them locally with all-MiniLM-L6-v2 (no API key; about 80 MB,
   downloaded once) into `data/processed/context/` (`context/store.py`).

```bash
cricassist build-context                 # fetch what the site allows, then build
cricassist build-context --no-fetch      # rebuild from cached pages only
cricassist build-context --local-only --source-dir path/to/docs   # documents you may use
```

**ESPNcricinfo and this project.** ESPNcricinfo's terms of use (Disney terms,
section 2.B(x)) forbid collecting its content by automated means for AI tools,
data mining or databases without written permission, and the site refuses
automated requests, including robots.txt (HTTP 403). The fetcher respects
that: it makes one request for robots.txt, is refused, and stops. It does not
disguise itself as a browser or work around the block, which is also why
`python-espncricinfo` (which bypasses the bot protection with a headless
browser) is not used. So the live context database is empty, and the assistant
says that no written context is available and answers from the stats. The
whole pipeline is tested on sample pages, and any source you have the right to
use can be loaded with `--source-dir`.

## Building the stats database

1. Unzip the dataset CSVs into `data/raw/` (see `src/cricassist/stats/dataset/README.md`).
2. Run:

   ```bash
   cricassist build-db
   ```

   This checks every CSV against the sizes, SHA-256 checksums and column
   headers in `manifest.json` and `schema.json`, loads them into DuckDB at
   `data/processed/cricassist.db`, then checks row counts and keys. It takes
   about a minute for the full dataset. Use `--skip-checksums` to check file
   sizes only.

Each CSV becomes a table of the same name. Column names are lowercased with
spaces replaced by underscores, so `striker balls faced` becomes
`striker_balls_faced`.

The build also creates derived tables for stats (`stats/views.py`):
`batting_innings` and `bowling_innings` (one row per player per innings),
`player_matches`, `deliveries` and `dismissals` (views without super overs),
and `bowling_styles`. They follow ESPNcricinfo's scoring conventions: super
overs are excluded, wides are not balls faced, byes and leg byes are not
charged to the bowler, and run outs are not credited to the bowler.

`stats/queries.py` has the stats functions, each with filters for format,
gender, competition, event, season, opposition, venue and dates:

- Players: batting and bowling summaries, recent innings, batter vs bowler,
  batter vs bowling style, phase stats (powerplay, middle, death), and line,
  length and shot breakdowns from ESPNcricinfo's ball tracking.
- Teams: head-to-heads with recent meetings and scores, results by
  opposition, and phase stats.
- Venues: average scores by innings, batting first vs chasing, toss, highest
  and lowest totals, and leading players.
- Records and leaderboards: highest scores, best figures, highest and lowest
  totals, and top players by any stat, overall or in one phase.

Renamed franchises count as one team under their current name (for example
Delhi Daredevils is Delhi Capitals), and short names like CSK work; see
`stats/teams.py`. Team and record queries default to men's cricket unless
the filters ask for women's.

`scripts/check_known_stats.py` checks 10 stats from the full database against
ESPNcricinfo's figures.

Cricsheet does not have every match, so totals can be lower than official
career figures. In particular the dataset has no Afghanistan internationals.
Every result says how many matches it is based on.

Tests run on a three-match subset in `tests/data/`, made with
`scripts/make_test_dataset.py`.
