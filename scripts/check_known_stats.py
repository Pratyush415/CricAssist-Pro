"""Check 10 stats from the full database against ESPNcricinfo's published figures.

Usage: python scripts/check_known_stats.py [path/to/cricassist.db]

The expected values are ESPNcricinfo's (Statsguru and scorecards). Each stat
was chosen because every match it depends on is in the dataset; see
KNOWN_GAPS at the bottom for stats that differ because matches are missing.
"""

import sys
from pathlib import Path

from cricassist.config import load_settings
from cricassist.stats.db import connect
from cricassist.stats.queries import (
    Filters,
    batting_summary,
    best_innings,
    bowling_summary,
    leaders,
    team_head_to_head,
)


def pick(d: dict, *keys):
    return tuple(d[k] for k in keys)


def first(records: list[dict], *keys):
    return pick(records[0], *keys)


CHECKS = [
    (
        "Virat Kohli, Test career: matches, runs, average, hundreds, highest",
        "Statsguru: V Kohli, Test batting",
        lambda c: pick(batting_summary(c, "Virat Kohli", Filters(format="TEST")), "matches", "runs", "average", "hundreds", "highest"),
        (123, 9230, 46.85, 30, "254*"),
    ),
    (
        "Alastair Cook, Test career: matches, runs, average, hundreds, highest",
        "Statsguru: AN Cook, Test batting",
        lambda c: pick(batting_summary(c, "Alastair Cook", Filters(format="TEST")), "matches", "runs", "average", "hundreds", "highest"),
        (161, 12472, 45.35, 33, "294"),
    ),
    (
        "R Ashwin, Tests: best innings, best match, five-fors, ten-wicket matches",
        "Statsguru: R Ashwin, Test bowling",
        lambda c: pick(bowling_summary(c, "R Ashwin", Filters(format="TEST")), "best_innings", "best_match", "five_wickets", "ten_wickets_in_match"),
        ("7/59", "13/140", 37, 8),
    ),
    (
        "Most runs in IPL 2016: Kohli's runs, hundreds, average, strike rate",
        "IPL 2016 most runs",
        lambda c: first(leaders(c, "runs", Filters(competition="ipl", season=2016), limit=1)["leaders"], "player", "runs", "hundreds", "average", "strike_rate"),
        ("V Kohli", 973, 4, 81.08, 152.03),
    ),
    (
        "Highest IPL score",
        "IPL records: highest individual score",
        lambda c: first(best_innings(c, "batting", Filters(competition="ipl"), limit=1)["records"], "player", "score", "opposition"),
        ("CH Gayle", "175* (66)", "Pune Warriors"),
    ),
    (
        "Highest men's ODI score",
        "ODI records: highest individual score",
        lambda c: first(best_innings(c, "batting", Filters(format="ODI", competition="international"), limit=1)["records"], "player", "score", "opposition", "date"),
        ("RG Sharma", "264 (173)", "Sri Lanka", "2014-11-13"),
    ),
    (
        "Highest IPL team total",
        "IPL records: highest totals",
        lambda c: first(best_innings(c, "team_highest", Filters(competition="ipl"), limit=1)["records"], "team", "score", "opposition", "date"),
        ("Sunrisers Hyderabad", "287/3 (20 ov)", "Royal Challengers Bengaluru", "2024-04-15"),
    ),
    (
        "Highest men's ODI team total",
        "ODI records: highest totals",
        lambda c: first(best_innings(c, "team_highest", Filters(format="ODI", competition="international"), limit=1)["records"], "team", "score", "opposition"),
        ("England", "498/4 (50 ov)", "Netherlands"),
    ),
    (
        "Lowest men's Test total since 2001, and the next lowest",
        "Test records: lowest totals",
        lambda c: tuple(
            (r["team"], r["score"], r["date"])
            for r in best_innings(c, "team_lowest", Filters(format="TEST"), limit=2)["records"]
        ),
        (("West Indies", "27", "2025-07-12"), ("India", "36/9", "2020-12-17")),
    ),
    (
        "2023 World Cup final: result, scores, player of the match",
        "Scorecard: India v Australia, Ahmedabad, 19 Nov 2023",
        lambda c: first(
            team_head_to_head(c, "India", "Australia", Filters(format="ODI", event="World Cup", season=2023), recent=1)["recent_meetings"],
            "result", "scores", "player_of_match",
        ),
        ("Australia won by 6 wickets", "India 240 (50 ov); Australia 241/4 (43 ov)", "TM Head"),
    ),
]

# Stats that differ from ESPNcricinfo because matches are missing from the
# dataset, not because of how they are computed.
KNOWN_GAPS = [
    "Bumrah, 2024 T20 World Cup: 12 wickets in 7 matches here, 15 in 8 on Cricinfo (no Afghanistan matches in the data).",
    "Kohli, T20Is: 3,963 runs here, 4,188 on Cricinfo (his 122* v Afghanistan in 2022 and other matches are missing).",
    "Ashwin, Tests: 532 wickets in 105 Tests here, 537 in 106 on Cricinfo.",
    "Anderson, Tests: 182 of 188 Tests here (Cricsheet lacks several 2003 Tests).",
]


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else load_settings().db_path
    con = connect(db_path)
    failures = 0
    for i, (label, source, compute, expected) in enumerate(CHECKS, 1):
        actual = compute(con)
        ok = actual == expected
        failures += not ok
        print(f"{i:>2}. {'PASS' if ok else 'FAIL'}  {label}")
        print(f"      ours:     {actual}")
        if not ok:
            print(f"      expected: {expected}  ({source})")
    print(f"\n{len(CHECKS) - failures} of {len(CHECKS)} match ESPNcricinfo.")
    print("\nKnown differences caused by missing matches:")
    for gap in KNOWN_GAPS:
        print(f"  - {gap}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
