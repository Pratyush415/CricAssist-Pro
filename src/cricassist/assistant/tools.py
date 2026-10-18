"""Tools the assistant can call: stats queries and ESPNcricinfo lookups.

Each tool is a stats query from `cricassist.stats.queries` with a name, a
description the model reads to decide when to use it, and a JSON schema for
its arguments. The same definitions serve every provider (see agent.py).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from cricassist.stats import queries as q

# --- Shared argument schemas -------------------------------------------------

FILTER_PROPERTIES = {
    "format": {"type": "string", "enum": ["TEST", "ODI", "T20"], "description": "Match format. The Hundred counts as T20."},
    "gender": {"type": "string", "enum": ["male", "female"], "description": "Men's or women's cricket. Team and record tools default to male."},
    "competition": {
        "type": "string",
        "description": "'international', or a league code: ipl, wpl, bbl, wbb (WBBL), psl, cpl, bpl, lpl, sat (SA20), ilt (ILT20), mlc, hnd (The Hundred).",
    },
    "event": {"type": "string", "description": "Part of the tournament name, e.g. 'T20 World Cup', 'Asia Cup', 'Border-Gavaskar'. Qualifiers are left out unless the text says qualifier."},
    "season": {"type": "string", "description": "A year like '2024' (matches starting that year), or a season label like '2023/24'."},
    "opposition": {"type": "string", "description": "The other team. Any name it has had or a short name (CSK, RCB, Aus) works."},
    "venue": {"type": "string", "description": "Part of the ground or city name, e.g. 'Wankhede', 'Lord's', 'Melbourne'."},
    "since": {"type": "string", "description": "First match date included, YYYY-MM-DD."},
    "until": {"type": "string", "description": "Last match date included, YYYY-MM-DD."},
}
FILTER_KEYS = tuple(FILTER_PROPERTIES)

PLAYER = {"type": "string", "description": "Player name as a journalist would write it ('Kohli', 'Jasprit Bumrah') or a Cricsheet player id."}
TEAM = {"type": "string", "description": "Team name, any earlier name, or short name (e.g. 'India', 'CSK', 'Delhi Daredevils')."}
ROLE = {"type": "string", "enum": ["batting", "bowling"]}


def _schema(properties: dict, required: list[str], filters: bool = True) -> dict:
    props = dict(properties)
    if filters:
        props.update(FILTER_PROPERTIES)
    return {"type": "object", "properties": props, "required": required}


def _filters(args: dict) -> q.Filters:
    return q.Filters(**{k: args[k] for k in FILTER_KEYS if args.get(k) not in (None, "")})


# --- Tools -------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable  # (con, args) -> dict


def _dataset_info(con, args: dict) -> dict:
    info = dict(con.execute("SELECT key, value FROM dataset_info").fetchall())
    formats = q._rows(
        con,
        """SELECT format, gender, type = 'Intl' AS international, count(*) AS matches,
                  min(start_date) AS first_match, max(start_date) AS last_match
           FROM matches GROUP BY ALL ORDER BY format, gender, international DESC""",
        [],
    )
    for f in formats:
        f["first_match"], f["last_match"] = str(f["first_match"]), str(f["last_match"])
    return {
        **info,
        "matches_by_format": formats,
        "what_the_data_has": "Ball-by-ball records of men's and women's Tests, ODIs and T20Is since Dec 2001, "
        "and the IPL, WPL, BBL, WBBL, PSL, CPL, BPL, LPL, SA20, ILT20, MLC and The Hundred.",
        "what_the_data_does_not_have": [
            "ICC rankings or ratings",
            "awards other than player of the match",
            "matches before Dec 2001, and domestic first-class, List A or other T20 leagues",
            "Afghanistan international matches (none at all)",
            "some other matches Cricsheet lacks, so career totals can be below official records",
            "news, quotes, injuries, squads or anything after the snapshot date",
        ],
    }


def _recent(con, args: dict) -> dict:
    fn = q.batting_innings_list if args.get("role", "batting") == "batting" else q.bowling_innings_list
    return fn(con, args["player"], _filters(args), last=int(args.get("last", 10)))


TOOLS = [
    Tool(
        "dataset_info",
        "What the stats database covers and what it does not (formats, leagues, dates, known gaps such as rankings "
        "and Afghanistan). Call this when unsure whether the data can answer a question.",
        _schema({}, [], filters=False),
        _dataset_info,
    ),
    Tool(
        "find_players",
        "Search players by part of a name. Use when a name is ambiguous or another tool reports more than one match.",
        _schema({"query": {"type": "string"}}, ["query"], filters=False),
        lambda con, a: {"players": q.find_players(con, a["query"])},
    ),
    Tool(
        "batting_summary",
        "A player's batting record: matches, innings, not outs, runs, highest score, average, strike rate, 100s, 50s, ducks, 4s, 6s.",
        _schema({"player": PLAYER}, ["player"]),
        lambda con, a: q.batting_summary(con, a["player"], _filters(a)),
    ),
    Tool(
        "bowling_summary",
        "A player's bowling record: wickets, average, economy, strike rate, best figures, 4-fors, 5-fors (and best match and 10-wicket matches for Tests).",
        _schema({"player": PLAYER}, ["player"]),
        lambda con, a: q.bowling_summary(con, a["player"], _filters(a)),
    ),
    Tool(
        "recent_innings",
        "A player's most recent innings (newest first) with scores or figures, for current form.",
        _schema({"player": PLAYER, "role": ROLE, "last": {"type": "integer", "description": "How many innings, default 10."}}, ["player"]),
        _recent,
    ),
    Tool(
        "player_by_year",
        "A player's record year by year, with a consistency summary (best and worst year, spread of yearly averages). "
        "Use for questions about consistency, form over time, or peak years.",
        _schema(
            {"player": PLAYER, "role": ROLE,
             "by_format": {"type": "boolean", "description": "Split each year by format."}},
            ["player", "role"],
        ),
        lambda con, a: q.player_by_year(con, a["player"], a.get("role", "batting"), _filters(a), bool(a.get("by_format", False))),
    ),
    Tool(
        "batter_vs_bowler",
        "Head-to-head between one batter and one bowler: balls, runs, dismissals by that bowler, average, strike rate, dot %.",
        _schema({"batter": PLAYER, "bowler": PLAYER}, ["batter", "bowler"]),
        lambda con, a: q.batter_vs_bowler(con, a["batter"], a["bowler"], _filters(a)),
    ),
    Tool(
        "batter_vs_bowling_style",
        "A batter's record against types of bowling. Without style: pace, spin and each kind. With style, one group: "
        + ", ".join(q.STYLE_GROUPS) + ".",
        _schema({"batter": PLAYER, "style": {"type": "string", "enum": list(q.STYLE_GROUPS)}}, ["batter"]),
        lambda con, a: q.batter_vs_bowling_style(con, a["batter"], _filters(a), a.get("style")),
    ),
    Tool(
        "player_phase_stats",
        "A player's batting or bowling split into powerplay, middle and death overs (ODIs and T20s only).",
        _schema({"player": PLAYER, "role": ROLE}, ["player", "role"]),
        lambda con, a: q.player_phase_stats(con, a["player"], a.get("role", "batting"), _filters(a)),
    ),
    Tool(
        "delivery_breakdown",
        "A player's record split by the ball's line or length, or by the shot played, from ESPNcricinfo ball tracking "
        "(covers some matches only, mostly 2014 on). by: length, line, shot (grouped) or shot_detail.",
        _schema(
            {"player": PLAYER, "role": ROLE,
             "by": {"type": "string", "enum": ["length", "line", "shot", "shot_detail"]}},
            ["player", "by"],
        ),
        lambda con, a: q.delivery_breakdown(con, a["player"], a["by"], a.get("role", "batting"), _filters(a)),
    ),
    Tool(
        "team_head_to_head",
        "Results between two teams (won, lost, tied, drawn, no result) and their latest meetings with scores.",
        _schema({"team": TEAM, "other_team": TEAM, "recent": {"type": "integer", "description": "How many recent meetings, default 5."}}, ["team", "other_team"]),
        lambda con, a: q.team_head_to_head(con, a["team"], a["other_team"], _filters(a), int(a.get("recent", 5))),
    ),
    Tool(
        "team_results",
        "A team's results overall and against each opponent.",
        _schema({"team": TEAM}, ["team"]),
        lambda con, a: q.team_results(con, a["team"], _filters(a)),
    ),
    Tool(
        "team_phase_stats",
        "A team's batting or bowling by phase (powerplay, middle, death): runs per innings, wickets per innings, strike or economy rate.",
        _schema({"team": TEAM, "role": ROLE}, ["team", "role"]),
        lambda con, a: q.team_phase_stats(con, a["team"], a.get("role", "batting"), _filters(a)),
    ),
    Tool(
        "venue_summary",
        "What a ground is like: average score by innings, batting first vs chasing, toss, highest and lowest totals, "
        "highest chases, top run-scorers and wicket-takers. Pass a format for meaningful averages.",
        _schema({"venue": {"type": "string", "description": "Part of the ground or city name."}}, ["venue"]),
        lambda con, a: q.venue_summary(con, a["venue"], _filters(a)),
    ),
    Tool(
        "leaders",
        "Top players by a stat, overall or in one phase. Batting: runs, average, strike_rate, sixes, fours, hundreds, fifties. "
        "Bowling: wickets, economy, bowling_average, bowling_strike_rate, dot_ball_pct.",
        _schema(
            {
                "stat": {"type": "string", "enum": sorted(q._BATTING_STATS | q._BOWLING_STATS)},
                "phase": {"type": "string", "enum": list(q.PHASES)},
                "min_balls": {"type": "integer", "description": "Qualifying balls for rate stats (default 120, or 60 in a phase)."},
                "limit": {"type": "integer", "description": "How many players, default 10."},
            },
            ["stat"],
        ),
        lambda con, a: q.leaders(con, a["stat"], _filters(a), a.get("phase"), a.get("min_balls"), int(a.get("limit", 10))),
    ),
    Tool(
        "best_innings",
        "Record lists: kind batting (highest individual scores), bowling (best figures), team_highest or team_lowest (totals).",
        _schema(
            {"kind": {"type": "string", "enum": ["batting", "bowling", "team_highest", "team_lowest"]},
             "limit": {"type": "integer", "description": "How many, default 10."}},
            ["kind"],
        ),
        lambda con, a: q.best_innings(con, a["kind"], _filters(a), int(a.get("limit", 10))),
    ),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def run_tool(con, name: str, args: dict) -> tuple[str, bool]:
    """Run a tool and return (JSON result, is_error). Errors the model can act
    on (unknown player, ambiguous team, bad argument) come back as results, not exceptions."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return json.dumps({"error": f"Unknown tool {name!r}"}), True
    try:
        result = tool.run(con, args or {})
        return json.dumps(result, default=_json_default), False
    except q.PlayerNotFound as e:
        return json.dumps({"error": str(e), "candidates": e.candidates[:8]}, default=_json_default), True
    except q.TeamNotFound as e:
        return json.dumps({"error": str(e), "candidates": e.candidates[:8]}), True
    except (ValueError, LookupError, KeyError, TypeError) as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"}), True
