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
