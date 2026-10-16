# CricAssist-Pro Roadmap

Deadline: **Wed Oct 21, 2026** (submission and demo)

## Goal for Oct 21

A command-line chat where a journalist asks questions like *"How has Kohli
done against left-arm spin in T20s since 2022?"* and gets an accurate,
sourced answer from the full ball-by-ball database, plus basic live lookups
from ESPNcricinfo.

## Plan

### Fri Oct 16: Data layer
- [x] Project structure, packaging, tests
- [x] Dataset `schema.json` and `manifest.json` committed
- [ ] DuckDB loader (`stats/db.py`, `stats/ingest.py`)
- [ ] Verify CSVs against manifest checksums and schema columns
- [ ] Tiny test dataset (2–3 matches) in `tests/`
- [ ] `cricassist build-db` command
- [ ] **Done when:** full database builds locally and row counts match the manifest

### Sat Oct 17: Stats engine, part 1
- [ ] Batting innings and bowling figures views
- [ ] Player career and recent form
- [ ] Batter vs bowler, batter vs bowling style
- [ ] **Done when:** these queries return correct numbers

### Sun Oct 18: Stats engine, part 2
- [ ] Team head-to-heads, venue records
- [ ] Phase stats (powerplay, middle overs, death overs)
- [ ] Line, length and shot breakdowns from tracking data
- [ ] **Done when:** 10 stats checked against ESPNcricinfo all match

### Mon Oct 19: AI assistant
- [ ] Claude API with tool use wrapping the stats queries
- [ ] System prompt: no invented numbers, always name the source and filters
- [ ] `cricassist chat` command
- [ ] **Done when:** end-to-end chat works

### Tue Oct 20: Cricinfo lookups and evaluation
- [ ] Check ESPNcricinfo terms of use before building any fetcher
- [ ] Live scorecard and player profile lookups (if allowed)
- [ ] Test set of 15 journalist questions with verified answers
- [ ] **Done when:** at least 13 of 15 answered correctly

### Wed Oct 21: Submission and demo
Submission:
- [ ] Fix remaining issues; all tests pass
- [ ] README: what it does, setup, how to build the database, how to run, architecture, data sources and licences
- [ ] Merge the work branch into `main` so the submitted repo is complete
- [ ] Fresh-clone check: setup steps work from scratch

Demo:
- [ ] Demo script: 5–6 rehearsed questions showing stats depth, sourcing and a live lookup
- [ ] Database pre-built and tested on the demo machine
- [ ] Fallback: screen recording of a full run, in case the network or API fails live
- [ ] Simple Streamlit web page (only if time allows)

## Deferred until after Oct 21
- Fetching editorial articles and news from ESPNcricinfo
- Full web app
- Automatic dataset refreshes and deployment
- `cricinfo_text_predictions.csv` and the small edge-case files

## Inputs needed
| When | What |
|---|---|
| Oct 16 | Small CSVs + 20,000-row samples of the two large CSVs |
| Oct 16–17 | Run `cricassist build-db` locally on the full dataset |
| By Oct 19 | Anthropic API key in local `.env` (never committed) |
| By Oct 20 | Decision on ESPNcricinfo access after checking terms of use |

## Risks
- **Wrong stats:** legal deliveries, retirements and super overs make stats easy
  to get subtly wrong. Mitigated by checking results against ESPNcricinfo.
- **ESPNcricinfo access** may not be allowed or may break. The demo still works
  fully on the offline data if so.
