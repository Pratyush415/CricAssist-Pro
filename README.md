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

Tests run on a three-match subset in `tests/data/`, made with
`scripts/make_test_dataset.py`.
