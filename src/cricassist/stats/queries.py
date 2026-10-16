"""Reusable stats queries (batting/bowling records, head-to-heads, venues).

Every function takes an open connection and returns plain dicts and lists,
ready to hand to the assistant as JSON. Each result carries the filters it
used and a `coverage` block saying how many matches it is based on, because
Cricsheet does not have every match: totals can be lower than official
career figures for players with matches missing from the data.
"""

from dataclasses import asdict, dataclass
from datetime import date

import duckdb

COVERAGE_NOTE = (
    "Computed from Cricsheet ball-by-ball data (matches from Dec 2001). "
    "Cricsheet does not have every match, so totals can be lower than official records."
)

# Kinds and groups a journalist can ask for when filtering by bowling style.
STYLE_GROUPS = {
    "pace": "bowler_kind = 'pace'",
    "spin": "bowler_kind IN ('off-spin', 'leg-spin', 'left-arm orthodox', 'left-arm wrist-spin')",
    "off-spin": "bowler_kind = 'off-spin'",
    "leg-spin": "bowler_kind = 'leg-spin'",
    "left-arm orthodox": "bowler_kind = 'left-arm orthodox'",
    "left-arm wrist-spin": "bowler_kind = 'left-arm wrist-spin'",
    "left-arm spin": "bowler_kind IN ('left-arm orthodox', 'left-arm wrist-spin')",
    "right-arm spin": "bowler_kind IN ('off-spin', 'leg-spin')",
    "left-arm pace": "bowler_kind = 'pace' AND bowler_arm = 'left-arm'",
    "right-arm pace": "bowler_kind = 'pace' AND bowler_arm = 'right-arm'",
}


class PlayerNotFound(LookupError):
    """No player, or more than one, matches the name given."""

    def __init__(self, query: str, candidates: list[dict]):
        self.query = query
        self.candidates = candidates
        what = "No player matches" if not candidates else "More than one player matches"
        super().__init__(f"{what} {query!r}")


@dataclass
class Filters:
    """Which matches a stat covers. Every field is optional."""

    format: str | None = None  # TEST, ODI or T20
    competition: str | None = None  # "international", or a league: ipl, bbl, psl, wpl, ...
    event: str | None = None  # part of the competition name, e.g. "World Cup"
    opposition: str | None = None  # the other team, e.g. "Australia"
    venue: str | None = None  # part of the ground or city name, e.g. "Lord's"
    since: date | str | None = None  # first day included
    until: date | str | None = None  # last day included

    def where(self, opposition_col: str = "opposition") -> tuple[str, list]:
        """SQL conditions (joined with AND, starting with AND) and their parameters."""
        sql, params = [], []
        if self.format:
            sql.append("format = ?")
            params.append(self.format.upper())
        if self.competition:
            if self.competition.lower() in ("international", "intl"):
                sql.append("type = 'Intl'")
            else:
                sql.append("collection = ?")
                params.append(self.competition.lower())
        if self.event:
            sql.append("event ILIKE ?")
            params.append(f"%{self.event}%")
        if self.opposition:
            sql.append(f"{opposition_col} ILIKE ?")
            params.append(self.opposition)
        if self.venue:
            sql.append("(venue ILIKE ? OR city ILIKE ?)")
            params += [f"%{self.venue}%"] * 2
        if self.since:
            sql.append("start_date >= ?")
            params.append(_as_date(self.since))
        if self.until:
            sql.append("start_date <= ?")
            params.append(_as_date(self.until))
        return "".join(f" AND {s}" for s in sql), params

    def describe(self) -> dict:
        return {k: str(v) for k, v in asdict(self).items() if v}


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def _rows(con, sql: str, params: list) -> list[dict]:
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _ratio(num, den, digits: int = 2):
    return round(num / den, digits) if den else None


def _overs(balls: int, balls_per_over: int = 6) -> str:
    return f"{balls // balls_per_over}.{balls % balls_per_over}" if balls % balls_per_over else str(balls // balls_per_over)


def _coverage(matches: int, first, last) -> dict:
    return {
        "matches_in_data": matches,
        "first_match": str(first) if first else None,
        "last_match": str(last) if last else None,
        "note": COVERAGE_NOTE,
    }


# --- Players -----------------------------------------------------------------


def find_players(con: duckdb.DuckDBPyConnection, query: str, limit: int = 10) -> list[dict]:
    """Players whose short or full name contains `query`, most-capped first."""
    words = query.split()
    cond = " AND ".join("(p.name ILIKE ? OR p.full_name ILIKE ?)" for _ in words) or "true"
    params = [w for word in words for w in (f"%{word}%", f"%{word}%")]
    return _rows(
        con,
        f"""SELECT p.player_id, p.name, p.full_name, p.country, p.gender, p.playing_role,
                   p.batting_style, p.bowling_style,
                   (SELECT count(*) FROM player_matches pm WHERE pm.player_id = p.player_id) AS matches_in_data
            FROM players p WHERE {cond}
            ORDER BY matches_in_data DESC, p.name LIMIT ?""",
        params + [limit],
    )


def resolve_player(con: duckdb.DuckDBPyConnection, player: str) -> dict:
    """A player by Cricsheet id, exact short or full name, or an unambiguous search."""
    exact = _rows(
        con,
        """SELECT player_id, name, full_name, country FROM players
           WHERE player_id = ? OR name ILIKE ? OR full_name ILIKE ?""",
        [player, player, player],
    )
    if len(exact) == 1:
        return exact[0]
    candidates = exact or find_players(con, player)
    if len(candidates) == 1:
        return candidates[0]
    # A clear favourite (e.g. "Kohli" -> Virat Kohli) wins over obscure namesakes.
    if not exact and len(candidates) > 1 and candidates[0]["matches_in_data"] >= 10 * max(
        1, candidates[1]["matches_in_data"]
    ):
        return candidates[0]
    raise PlayerNotFound(player, candidates)


# --- Batting -----------------------------------------------------------------


def batting_summary(con, player: str, filters: Filters | None = None) -> dict:
    """Career-style batting record: matches, innings, runs, average, strike rate, hundreds..."""
    filters = filters or Filters()
    p = resolve_player(con, player)
    where, params = filters.where()
    matches = con.execute(
        f"SELECT count(*), min(start_date), max(start_date) FROM player_matches WHERE player_id = ?{where}",
        [p["player_id"], *params],
    ).fetchone()
    r = _rows(
        con,
        f"""SELECT count(*) AS innings,
                   count(*) FILTER (WHERE NOT is_out) AS not_outs,
                   coalesce(sum(runs), 0) AS runs,
                   coalesce(sum(balls), 0) AS balls,
                   count(*) FILTER (WHERE is_out) AS dismissals,
                   arg_max(runs || CASE WHEN is_out THEN '' ELSE '*' END, runs * 2 + (NOT is_out)::INT) AS highest,
                   count(*) FILTER (WHERE runs >= 100) AS hundreds,
                   count(*) FILTER (WHERE runs BETWEEN 50 AND 99) AS fifties,
                   count(*) FILTER (WHERE runs = 0 AND is_out) AS ducks,
                   coalesce(sum(fours), 0) AS fours,
                   coalesce(sum(sixes), 0) AS sixes
            FROM batting_innings WHERE player_id = ?{where}""",
        [p["player_id"], *params],
    )[0]
    dismissals = r.pop("dismissals")
    return {
        "player": p,
        "filters": filters.describe(),
        "matches": matches[0],
        **r,
        "average": _ratio(r["runs"], dismissals),
        "strike_rate": _ratio(100 * r["runs"], r["balls"]),
        "coverage": _coverage(matches[0], matches[1], matches[2]),
    }


def batting_innings_list(con, player: str, filters: Filters | None = None, last: int = 10) -> dict:
    """The player's most recent innings (newest first), for recent form."""
    filters = filters or Filters()
    p = resolve_player(con, player)
    where, params = filters.where()
    rows = _rows(
        con,
        f"""SELECT start_date AS date, format, event, team, opposition, venue, innings, position,
                   runs, balls, fours, sixes, is_out, how_out, dismissed_by
            FROM batting_innings WHERE player_id = ?{where}
            ORDER BY start_date DESC, match_id DESC, innings DESC LIMIT ?""",
        [p["player_id"], *params, last],
    )
    for row in rows:
        row["date"] = str(row["date"])
        row["score"] = f"{row['runs']}{'' if row['is_out'] else '*'} ({row['balls']})"
    runs = sum(r["runs"] for r in rows)
    outs = sum(r["is_out"] for r in rows)
    balls = sum(r["balls"] for r in rows)
    return {
        "player": p,
        "filters": filters.describe(),
        "innings": rows,
        "totals": {
            "innings": len(rows),
            "runs": runs,
            "average": _ratio(runs, outs),
            "strike_rate": _ratio(100 * runs, balls),
        },
        "note": COVERAGE_NOTE,
    }


# --- Bowling -----------------------------------------------------------------


def bowling_summary(con, player: str, filters: Filters | None = None) -> dict:
    """Career-style bowling record: wickets, average, economy, best figures, five-fors..."""
    filters = filters or Filters()
    p = resolve_player(con, player)
    where, params = filters.where()
    matches = con.execute(
        f"SELECT count(*), min(start_date), max(start_date) FROM player_matches WHERE player_id = ?{where}",
        [p["player_id"], *params],
    ).fetchone()
    r = _rows(
        con,
        f"""SELECT count(*) AS innings,
                   coalesce(sum(balls), 0) AS balls,
                   coalesce(sum(maidens), 0) AS maidens,
                   coalesce(sum(runs), 0) AS runs,
                   coalesce(sum(wickets), 0) AS wickets,
                   arg_max(wickets || '/' || runs, wickets * 100000 - runs) AS best_innings,
                   count(*) FILTER (WHERE wickets = 4) AS four_wickets,
                   count(*) FILTER (WHERE wickets >= 5) AS five_wickets,
                   coalesce(sum(dots), 0) AS dots,
                   coalesce(sum(wides), 0) AS wides,
                   coalesce(sum(noballs), 0) AS noballs
            FROM bowling_innings WHERE player_id = ?{where}""",
        [p["player_id"], *params],
    )[0]
    by_match = con.execute(
        f"""SELECT arg_max(w || '/' || r, w * 100000 - r), count(*) FILTER (WHERE w >= 10)
            FROM (SELECT match_id, sum(wickets) AS w, sum(runs) AS r
                  FROM bowling_innings WHERE player_id = ?{where} GROUP BY match_id)""",
        [p["player_id"], *params],
    ).fetchone()
    result = {
        "player": p,
        "filters": filters.describe(),
        "matches": matches[0],
        **r,
        "overs": _overs(r["balls"]),
        "average": _ratio(r["runs"], r["wickets"]),
        "economy": _ratio(6 * r["runs"], r["balls"]),
        "strike_rate": _ratio(r["balls"], r["wickets"], 1),
        "dot_ball_pct": _ratio(100 * r["dots"], r["balls"], 1),
    }
    if (filters.format or "").upper() == "TEST":
        result["best_match"] = by_match[0]
        result["ten_wickets_in_match"] = by_match[1]
    result["coverage"] = _coverage(matches[0], matches[1], matches[2])
    return result


def bowling_innings_list(con, player: str, filters: Filters | None = None, last: int = 10) -> dict:
    """The player's most recent bowling innings (newest first), for recent form."""
    filters = filters or Filters()
    p = resolve_player(con, player)
    where, params = filters.where()
    rows = _rows(
        con,
        f"""SELECT start_date AS date, format, event, team, opposition, venue, innings,
                   balls, balls_per_over, maidens, runs, wickets
            FROM bowling_innings WHERE player_id = ?{where}
            ORDER BY start_date DESC, match_id DESC, innings DESC LIMIT ?""",
        [p["player_id"], *params, last],
    )
    for row in rows:
        row["date"] = str(row["date"])
        row["figures"] = f"{_overs(row['balls'], row.pop('balls_per_over'))}-{row['maidens']}-{row['runs']}-{row['wickets']}"
    runs = sum(r["runs"] for r in rows)
    wickets = sum(r["wickets"] for r in rows)
    balls = sum(r["balls"] for r in rows)
    return {
        "player": p,
        "filters": filters.describe(),
        "innings": rows,
        "totals": {
            "innings": len(rows),
            "wickets": wickets,
            "runs": runs,
            "average": _ratio(runs, wickets),
            "economy": _ratio(6 * runs, balls),
        },
        "note": COVERAGE_NOTE,
    }


# --- Match-ups ---------------------------------------------------------------

_MATCHUP_COLUMNS = """
    count(DISTINCT match_id || '-' || innings) AS innings,
    count(*) FILTER (WHERE faced) AS balls,
    coalesce(sum(runs_off_bat), 0) AS runs,
    count(*) FILTER (WHERE faced AND runs_off_bat = 0) AS dots,
    count(*) FILTER (WHERE is_four) AS fours,
    count(*) FILTER (WHERE is_six) AS sixes,
    count(*) FILTER (WHERE dismissed) AS dismissals
"""

# Deliveries faced by one batter, with whether the bowler dismissed them on it.
_FACED = """
    SELECT d.*, EXISTS (
        SELECT 1 FROM dismissals x
        WHERE x.match_id = d.match_id AND x.innings = d.innings
          AND x.delivery_number = d.delivery_number
          AND x.player_id = d.striker_id AND x.bowler_credited
    ) AS dismissed
    FROM deliveries d WHERE d.striker_id = ?
"""


def _matchup_stats(row: dict) -> dict:
    row["average"] = _ratio(row["runs"], row["dismissals"])
    row["strike_rate"] = _ratio(100 * row["runs"], row["balls"])
    row["dot_ball_pct"] = _ratio(100 * row["dots"], row["balls"], 1)
    row["balls_per_dismissal"] = _ratio(row["balls"], row["dismissals"], 1)
    return row


def batter_vs_bowler(con, batter: str, bowler: str, filters: Filters | None = None) -> dict:
    """Head-to-head: what the batter scored off the bowler, and how often the bowler got them out.

    Only dismissals credited to the bowler count (a run out off their bowling does not).
    """
    filters = filters or Filters()
    bat, bowl = resolve_player(con, batter), resolve_player(con, bowler)
    where, params = filters.where(opposition_col="bowling_team")
    r = _rows(
        con,
        f"""SELECT {_MATCHUP_COLUMNS}, count(DISTINCT match_id) AS matches,
                   min(start_date) AS first_match, max(start_date) AS last_match
            FROM ({_FACED}) WHERE bowler_id = ?{where}""",
        [bat["player_id"], bowl["player_id"], *params],
    )[0]
    coverage = _coverage(r.pop("matches"), r.pop("first_match"), r.pop("last_match"))
    return {"batter": bat, "bowler": bowl, "filters": filters.describe(), **_matchup_stats(r), "coverage": coverage}


def batter_vs_bowling_style(con, batter: str, filters: Filters | None = None, style: str | None = None) -> dict:
    """The batter's record against each type of bowling (pace, off-spin, leg-spin, ...).

    With `style` (one of STYLE_GROUPS, e.g. "left-arm spin") returns that one group,
    otherwise a row per arm and kind of bowling plus pace and spin totals.
    """
    filters = filters or Filters()
    bat = resolve_player(con, batter)
    where, params = filters.where(opposition_col="bowling_team")
    base = f"FROM ({_FACED}) WHERE true{where}"
    args = [bat["player_id"], *params]

    if style:
        key = style.lower().strip()
        if key not in STYLE_GROUPS:
            raise ValueError(f"Unknown bowling style {style!r}. Use one of: {', '.join(STYLE_GROUPS)}")
        groups = {key: STYLE_GROUPS[key]}
    else:
        groups = {g: STYLE_GROUPS[g] for g in ("pace", "spin")}

    rows = []
    for name, cond in groups.items():
        row = _rows(con, f"SELECT {_MATCHUP_COLUMNS} {base} AND {cond}", args)[0]
        rows.append({"style": name, **_matchup_stats(row)})
    if not style:
        detail = _rows(
            con,
            f"""SELECT CASE WHEN bowler_kind = 'pace' THEN bowler_arm || ' pace' ELSE bowler_kind END AS style,
                   {_MATCHUP_COLUMNS}
                {base} AND bowler_kind IS NOT NULL AND bowler_kind <> 'other'
                GROUP BY ALL ORDER BY balls DESC""",
            args,
        )
        rows += [_matchup_stats(row) for row in detail]

    span = con.execute(f"SELECT count(DISTINCT match_id), min(start_date), max(start_date) {base}", args).fetchone()
    return {
        "batter": bat,
        "filters": filters.describe(),
        "by_style": rows,
        "coverage": _coverage(*span),
        "style_note": "Bowlers are grouped by their main listed bowling style; bowlers with no style on record are left out.",
    }
