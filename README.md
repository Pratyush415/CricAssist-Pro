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
