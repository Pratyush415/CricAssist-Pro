"""Reusable stats queries (batting/bowling records, head-to-heads, venues).

Every function takes an open connection and returns plain dicts and lists,
ready to hand to the assistant as JSON. Each result carries the filters it
used and a `coverage` block saying how many matches it is based on, because
Cricsheet does not have every match: totals can be lower than official
career figures for players with matches missing from the data.
"""

import re
from dataclasses import asdict, dataclass, replace
from datetime import date

import duckdb

COVERAGE_NOTE = (
    "Computed from Cricsheet ball-by-ball data (matches from Dec 2001). "
    "The dataset does not have every match (it has no Afghanistan internationals at all), "
    "so totals can be lower than official records."
)

# Teams that play international cricket but have no international matches in
# the dataset, with what to tell the journalist.
MISSING_TEAMS = {
    "afghanistan": (
        "The dataset has no Afghanistan international matches, so Afghanistan's team "
        "record and every international against Afghanistan are missing. Afghanistan "
        "players' franchise league matches are included."
    ),
}

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


class TeamNotFound(LookupError):
    """No team, or more than one, matches the name given."""

    def __init__(self, query: str, candidates: list[str], reason: str | None = None):
        self.query = query
        self.candidates = candidates
        self.reason = reason
        what = "No team matches" if not candidates else "More than one team matches"
        super().__init__(f"{what} {query!r}" + (f": {reason}" if reason else ""))


# Teams named in SQL: every name the team has had (see teams.py).
_TEAM_NAMES_SQL = "(SELECT name FROM team_names WHERE team = ?)"


def _normalise(text: str) -> str:
    """Lowercase letters and digits only, so "M.Chinnaswamy" matches "M Chinnaswamy"."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass
class Filters:
    """Which matches a stat covers. Every field is optional."""

    format: str | None = None  # TEST, ODI or T20
    gender: str | None = None  # male or female; team and record queries default to male
    competition: str | None = None  # "international", or a league: ipl, bbl, psl, wpl, ...
    event: str | None = None  # part of the competition name, e.g. "World Cup"
    season: str | int | None = None  # a year (2024), or a season label like "2023/24"
    opposition: str | None = None  # the other team: any name it has had, or a short name like "CSK"
    venue: str | None = None  # part of the ground or city name, e.g. "Lord's"
    since: date | str | None = None  # first day included
    until: date | str | None = None  # last day included

    def where(self, opposition_col: str = "opposition") -> tuple[str, list]:
        """SQL conditions (joined with AND, starting with AND) and their parameters."""
        sql, params = [], []
        if self.format:
            sql.append("format = ?")
            params.append(self.format.upper())
        if self.gender:
            sql.append("gender = ?")
            params.append({"men": "male", "women": "female"}.get(self.gender.lower(), self.gender.lower()))
        if self.competition:
            if self.competition.lower() in ("international", "intl"):
                sql.append("type = 'Intl'")
            else:
                sql.append("collection = ?")
                params.append(self.competition.lower())
        if self.event:
            sql.append("event ILIKE ?")
            params.append(f"%{self.event}%")
            # "T20 World Cup" means the tournament, not its qualifiers, unless asked.
            if "qualif" not in self.event.lower():
                sql.append("event NOT ILIKE '%qualif%'")
        if self.season:
            if "/" in str(self.season):
                sql.append("season = ?")
                params.append(str(self.season))
            else:
                sql.append("year(start_date) = ?")
                params.append(int(self.season))
        if self.opposition:
            # Call resolve() first so this is the team's current name.
            sql.append(f"{opposition_col} IN {_TEAM_NAMES_SQL}")
            params.append(self.opposition)
        if self.venue:
            sql.append("regexp_replace(lower(venue || ' ' || coalesce(city, '')), '[^a-z0-9]', '', 'g') LIKE ?")
            params.append(f"%{_normalise(self.venue)}%")
        if self.since:
            sql.append("start_date >= ?")
            params.append(_as_date(self.since))
        if self.until:
            sql.append("start_date <= ?")
            params.append(_as_date(self.until))
        return "".join(f" AND {s}" for s in sql), params

    def describe(self) -> dict:
        return {k: str(v) for k, v in asdict(self).items() if v}

    def resolve(self, con) -> "Filters":
        """A copy with the opposition replaced by its current team name."""
        if not self.opposition:
            return self
        return replace(self, opposition=resolve_team(con, self.opposition))


def _filters(con, filters: "Filters | None") -> "Filters":
    return (filters or Filters()).resolve(con)


_WOMENS_LEAGUES = ("wpl", "wbb")


def _team_filters(con, filters: "Filters | None") -> "Filters":
    """Filters for team and record queries. Team names like "India" are shared by
    the men's and women's sides, so these default to men's cricket unless the
    filters say otherwise (a gender, a women's league, or "women" in the event)."""
    filters = _filters(con, filters)
    womens = (filters.competition or "").lower() in _WOMENS_LEAGUES or "women" in (filters.event or "").lower()
    if filters.gender or womens:
        return filters if filters.gender or not womens else replace(filters, gender="female")
    return replace(filters, gender="male")


def resolve_team(con: duckdb.DuckDBPyConnection, team: str) -> str:
    """A team's current name, from any name it has had, a short name, or part of a name."""
    exact = con.execute(
        "SELECT DISTINCT team FROM team_names WHERE lower(name) = lower(?)", [team]
    ).fetchall()
    if len(exact) == 1:
        return exact[0][0]
    found = [
        r[0]
        for r in con.execute(
            """SELECT DISTINCT team FROM team_names
               WHERE regexp_replace(lower(name), '[^a-z0-9]', '', 'g') LIKE ? ORDER BY team""",
            [f"%{_normalise(team)}%"],
        ).fetchall()
    ]
    if len(found) == 1:
        return found[0]
    raise TeamNotFound(team, found, MISSING_TEAMS.get(team.strip().lower()))


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
    if len(exact) > 1:
        # Namesakes: rank by matches, and let a clear favourite win (Rashid Khan of Afghanistan).
        ids = [e["player_id"] for e in exact]
        exact = _rows(
            con,
            f"""SELECT p.player_id, p.name, p.full_name, p.country,
                       (SELECT count(*) FROM player_matches pm WHERE pm.player_id = p.player_id) AS matches_in_data
                FROM players p WHERE p.player_id IN ({", ".join("?" * len(ids))})
                ORDER BY matches_in_data DESC""",
            ids,
        )
        if exact[0]["matches_in_data"] >= 10 * max(1, exact[1]["matches_in_data"]):
            return exact[0]
    candidates = exact or find_players(con, player)
    if len(candidates) == 1:
        return candidates[0]
    if not exact and len(candidates) > 1:
        # Whole-word matches beat partial ones: "Shami" is Mohammed Shami, not Shamim Hossain.
        words = [re.escape(w) for w in player.lower().split()]
        whole = [
            c for c in candidates
            if all(re.search(rf"\b{w}\b", f"{c['name']} {c['full_name'] or ''}".lower()) for w in words)
        ]
        if len(whole) == 1:
            return whole[0]
        pool = whole or candidates
        # A clear favourite (e.g. "Kohli" -> Virat Kohli) wins over obscure namesakes.
        if len(pool) > 1 and pool[0]["matches_in_data"] >= 10 * max(1, pool[1]["matches_in_data"]):
            return pool[0]
    raise PlayerNotFound(player, candidates)


# --- Batting -----------------------------------------------------------------


def batting_summary(con, player: str, filters: Filters | None = None) -> dict:
    """Career-style batting record: matches, innings, runs, average, strike rate, hundreds..."""
    filters = _filters(con, filters)
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
    filters = _filters(con, filters)
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
    filters = _filters(con, filters)
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
    filters = _filters(con, filters)
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
    filters = _filters(con, filters)
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
    filters = _filters(con, filters)
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


# --- Teams -------------------------------------------------------------------


def _score(runs: int, wickets: int, declared: bool, balls: int, balls_per_over: int, fmt: str) -> str:
    """A total as a scorecard writes it: 233, 285/9d, 188/5 (20 ov)."""
    text = str(runs) if wickets >= 10 else f"{runs}/{wickets}"
    if declared:
        text += "d"
    if fmt != "TEST":
        text += f" ({_overs(balls, balls_per_over)} ov)"
    return text


def _match_scores(con, match_ids: list[int]) -> dict[int, str]:
    """Each match's innings totals, e.g. "Bangladesh 233 & 146; India 285/9d & 98/3"."""
    if not match_ids:
        return {}
    rows = con.execute(
        f"""SELECT match_id, team, runs, wickets, declared, balls, balls_per_over, format
            FROM team_innings WHERE match_id IN ({", ".join("?" * len(match_ids))})
            ORDER BY match_id, innings""",
        match_ids,
    ).fetchall()
    by_match: dict[int, dict[str, list[str]]] = {}
    for match_id, team, runs, wickets, declared, balls, bpo, fmt in rows:
        by_match.setdefault(match_id, {}).setdefault(team, []).append(
            _score(runs, wickets, declared, balls, bpo, fmt)
        )
    return {m: "; ".join(f"{t} {' & '.join(s)}" for t, s in teams.items()) for m, teams in by_match.items()}


_RESULT_COUNTS = """
    count(*) AS played,
    count(*) FILTER (WHERE result = 'won') AS won,
    count(*) FILTER (WHERE result = 'lost') AS lost,
    count(*) FILTER (WHERE result = 'tied') AS tied,
    count(*) FILTER (WHERE won_tiebreak) AS ties_won_in_super_over,
    count(*) FILTER (WHERE result = 'drawn') AS drawn,
    count(*) FILTER (WHERE result = 'no result') AS no_result,
    min(start_date) AS first_match, max(start_date) AS last_match
"""


def _results_block(row: dict) -> dict:
    decided = row["won"] + row["lost"]
    row["win_pct"] = _ratio(100 * row["won"], decided, 1)
    row["first_match"] = str(row["first_match"]) if row["first_match"] else None
    row["last_match"] = str(row["last_match"]) if row["last_match"] else None
    return row


def team_head_to_head(con, team: str, opposition: str, filters: Filters | None = None, recent: int = 5) -> dict:
    """Results between two teams (won, lost, tied, drawn, no result) and their latest meetings.

    Renamed franchises count as one team under their current name.
    """
    filters = replace(_team_filters(con, filters), opposition=None)
    a, b = resolve_team(con, team), resolve_team(con, opposition)
    where, params = filters.where()
    base = f"FROM match_results WHERE team = ? AND opposition = ?{where}"
    args = [a, b, *params]
    summary = _results_block(_rows(con, f"SELECT {_RESULT_COUNTS} {base}", args)[0])
    meetings = _rows(
        con,
        f"""SELECT match_id, start_date AS date, format, event, venue, result, won_tiebreak, margin, player_of_match
            {base} ORDER BY start_date DESC, match_id DESC LIMIT ?""",
        [*args, recent],
    )
    scores = _match_scores(con, [m["match_id"] for m in meetings])
    for m in meetings:
        m["date"] = str(m["date"])
        m["scores"] = scores.get(m["match_id"], "")
        winner = a if m["result"] == "won" else b if m["result"] == "lost" else None
        m["result"] = f"{winner} won by {m['margin']}" if winner and m["margin"] else (
            f"{winner} won" if winner else m["result"]
        )
        if m.pop("won_tiebreak"):
            m["result"] += f" ({a} won the super over)"
    return {
        "team": a,
        "opposition": b,
        "filters": filters.describe(),
        "results_for_team": summary,
        "recent_meetings": meetings,
        "note": COVERAGE_NOTE,
    }


def team_results(con, team: str, filters: Filters | None = None) -> dict:
    """A team's results overall and against each opponent."""
    filters = _team_filters(con, filters)
    t = resolve_team(con, team)
    where, params = filters.where()
    base = f"FROM match_results WHERE team = ?{where}"
    args = [t, *params]
    overall = _results_block(_rows(con, f"SELECT {_RESULT_COUNTS} {base}", args)[0])
    by_opposition = [
        _results_block(r)
        for r in _rows(con, f"SELECT opposition, {_RESULT_COUNTS} {base} GROUP BY opposition ORDER BY played DESC", args)
    ]
    return {"team": t, "filters": filters.describe(), "overall": overall, "by_opposition": by_opposition, "note": COVERAGE_NOTE}


# --- Venues ------------------------------------------------------------------


def venue_summary(con, venue: str, filters: Filters | None = None, top: int = 5) -> dict:
    """What a ground is like: average scores by innings, batting first vs chasing,
    toss, highest and lowest totals, highest chase, and its leading players."""
    filters = replace(_team_filters(con, filters), venue=venue)
    where, params = filters.where()
    grounds = [r[0] for r in con.execute(f"SELECT DISTINCT venue FROM team_innings WHERE true{where} ORDER BY 1", params).fetchall()]
    if not grounds:
        raise LookupError(f"No matches at a venue matching {venue!r} with these filters")

    by_innings = _rows(
        con,
        f"""SELECT innings, count(*) AS innings_played, round(avg(runs), 1) AS average_score,
                   round(avg(wickets), 1) AS average_wickets,
                   round(6.0 * sum(runs) / nullif(sum(balls), 0), 2) AS run_rate
            FROM team_innings WHERE balls > 0 AND result <> 'no result'{where}
            GROUP BY innings ORDER BY innings""",
        params,
    )
    results = _rows(
        con,
        f"""SELECT count(*) AS matches,
                   count(*) FILTER (WHERE result = 'won') AS won_batting_first,
                   count(*) FILTER (WHERE result = 'lost') AS won_batting_second,
                   count(*) FILTER (WHERE result = 'tied') AS tied,
                   count(*) FILTER (WHERE result = 'drawn') AS drawn,
                   count(*) FILTER (WHERE result = 'no result') AS no_result
            FROM team_innings WHERE innings = 1{where}""",
        params,
    )[0]
    toss = con.execute(
        f"""SELECT count(*) FILTER (WHERE result = 'won'), count(*) FILTER (WHERE result IN ('won', 'lost')),
                   count(*) FILTER (WHERE toss_decision = 'bat'), count(*)
            FROM match_results WHERE won_toss{where}""",
        params,
    ).fetchone()
    results["toss_winner_won_pct"] = _ratio(100 * toss[0], toss[1], 1)
    results["toss_winner_chose_to_bat_pct"] = _ratio(100 * toss[2], toss[3], 1)

    def totals(order: str, cond: str = "true") -> list[dict]:
        rows = _rows(
            con,
            f"""SELECT team, opposition, runs, wickets, declared, balls, balls_per_over, format, start_date AS date, result
                FROM team_innings WHERE balls > 0 AND {cond}{where} ORDER BY {order} LIMIT 3""",
            params,
        )
        return [
            {
                "team": r["team"],
                "score": _score(r["runs"], r["wickets"], r["declared"], r["balls"], r["balls_per_over"], r["format"]),
                "opposition": r["opposition"],
                "date": str(r["date"]),
                "result": r["result"],
            }
            for r in rows
        ]

    top_batters = leaders(con, "runs", filters, limit=top)["leaders"]
    top_bowlers = leaders(con, "wickets", filters, limit=top)["leaders"]
    span = con.execute(f"SELECT count(DISTINCT match_id), min(start_date), max(start_date) FROM team_innings WHERE true{where}", params).fetchone()
    return {
        "venue": venue,
        "grounds_matched": grounds,
        "filters": filters.describe(),
        "results": results,
        "by_innings": by_innings,
        "highest_totals": totals("runs DESC"),
        "lowest_completed_totals": totals("runs ASC", _COMPLETED),
        # A successful chase: the last innings of a match, won by the side batting.
        "highest_successful_chases": totals("runs DESC", "is_last_innings AND innings > 1 AND result = 'won'"),
        "top_run_scorers": top_batters,
        "top_wicket_takers": top_bowlers,
        "coverage": _coverage(*span),
        "format_note": None if filters.format else "Formats are mixed; pass a format for meaningful averages.",
    }


# An innings that ran its course: all out, overs used up, or (in a Test) ended
# short of ten wickets because batters could not bat, as in India's 36/9 in 2020.
_COMPLETED = """(
    wickets >= 10
    OR (format = 'TEST' AND NOT declared AND NOT is_last_innings)
    OR (format <> 'TEST' AND balls >= coalesce(target_overs, scheduled_overs) * balls_per_over)
)"""


def best_innings(con, kind: str, filters: Filters | None = None, limit: int = 10) -> dict:
    """Record lists: kind is "batting" (highest scores), "bowling" (best figures),
    "team_highest" or "team_lowest" (completed innings only)."""
    filters = _team_filters(con, filters)
    if kind == "batting":
        where, params = filters.where()
        rows = _rows(
            con,
            f"""SELECT match_id, player, team, opposition, runs, balls, fours, sixes, is_out, start_date AS date, format, event, venue
                FROM batting_innings WHERE true{where} ORDER BY runs DESC, balls ASC, start_date LIMIT ?""",
            [*params, limit],
        )
        for r in rows:
            r["score"] = f"{r['runs']}{'' if r.pop('is_out') else '*'} ({r['balls']})"
            r["strike_rate"] = _ratio(100 * r["runs"], r["balls"])
    elif kind == "bowling":
        where, params = filters.where()
        rows = _rows(
            con,
            f"""SELECT match_id, player, team, opposition, balls, balls_per_over, maidens, runs, wickets,
                       start_date AS date, format, event, venue
                FROM bowling_innings WHERE true{where} ORDER BY wickets DESC, runs ASC, start_date LIMIT ?""",
            [*params, limit],
        )
        for r in rows:
            r["figures"] = f"{r['wickets']}/{r['runs']} ({_overs(r['balls'], r.pop('balls_per_over'))} ov)"
    elif kind in ("team_highest", "team_lowest"):
        where, params = filters.where()
        cond = _COMPLETED if kind == "team_lowest" else "balls > 0"
        order = "runs ASC" if kind == "team_lowest" else "runs DESC"
        rows = _rows(
            con,
            f"""SELECT match_id, team, opposition, runs, wickets, declared, balls, balls_per_over, format,
                       innings, result, start_date AS date, event, venue
                FROM team_innings WHERE {cond}{where} ORDER BY {order}, start_date LIMIT ?""",
            [*params, limit],
        )
        for r in rows:
            r["score"] = _score(r["runs"], r["wickets"], r.pop("declared"), r["balls"], r.pop("balls_per_over"), r["format"])
    else:
        raise ValueError("kind must be batting, bowling, team_highest or team_lowest")
    for r in rows:
        r["date"] = str(r["date"])
    return {"kind": kind, "filters": filters.describe(), "records": rows, "coverage": _match_coverage(con, filters)}


def _match_coverage(con, filters: Filters) -> dict:
    """How many matches the filters cover, and their date range."""
    where, params = filters.where()
    return _coverage(*con.execute(
        f"SELECT count(DISTINCT match_id), min(start_date), max(start_date) FROM team_innings WHERE true{where}", params
    ).fetchone())


# --- Leaderboards ------------------------------------------------------------


def _filtered(source: str, filters: Filters, opposition_col: str) -> tuple[str, list]:
    """`source` (deliveries or tracked_deliveries) narrowed by the filters, as a subquery."""
    where, params = filters.where(opposition_col=opposition_col)
    return f"(SELECT * FROM {source} WHERE true{where})", params


_BATTING_STATS = {"runs", "average", "strike_rate", "sixes", "fours", "hundreds", "fifties"}
_BOWLING_STATS = {"wickets", "economy", "bowling_average", "bowling_strike_rate", "dot_ball_pct"}
# Sort direction: lower is better for these.
_LOWER_IS_BETTER = {"economy", "bowling_average", "bowling_strike_rate"}
PHASES = ("powerplay", "middle", "death")


def leaders(
    con,
    stat: str,
    filters: Filters | None = None,
    phase: str | None = None,
    min_balls: int | None = None,
    limit: int = 10,
) -> dict:
    """Top players by a stat, e.g. most runs in IPL 2025 or best death-over economy.

    Batting stats: runs, average, strike_rate, sixes, fours, hundreds, fifties.
    Bowling stats: wickets, economy, bowling_average, bowling_strike_rate, dot_ball_pct.
    Rate stats need a minimum number of balls (default 60 with a phase, 120 without)
    so a player with one lucky over does not top the list.
    """
    filters = _team_filters(con, filters)
    if phase and phase not in PHASES:
        raise ValueError(f"phase must be one of {', '.join(PHASES)}")
    if stat not in _BATTING_STATS | _BOWLING_STATS:
        raise ValueError(f"Unknown stat {stat!r}. Use one of: {', '.join(sorted(_BATTING_STATS | _BOWLING_STATS))}")
    if phase and stat in ("hundreds", "fifties"):
        raise ValueError("hundreds and fifties are whole-innings stats; leave out the phase")
    batting = stat in _BATTING_STATS
    is_rate = stat in ("average", "strike_rate", "economy", "bowling_average", "bowling_strike_rate", "dot_ball_pct")
    if min_balls is None:
        min_balls = (60 if phase else 120) if is_rate else 0

    if batting:
        where, params = filters.where()
        if phase:
            sub, params = _filtered("deliveries", filters, "bowling_team")
            source = f"""
                SELECT d.striker_id AS player_id, count(DISTINCT d.match_id) AS matches,
                       count(DISTINCT d.match_id || '-' || d.innings) AS innings,
                       sum(d.runs_off_bat) AS runs, count(*) FILTER (WHERE d.faced) AS balls,
                       count(*) FILTER (WHERE d.is_four) AS fours, count(*) FILTER (WHERE d.is_six) AS sixes,
                       count(x.player_id) AS outs
                FROM {sub} d
                LEFT JOIN dismissals x ON x.match_id = d.match_id AND x.innings = d.innings
                     AND x.delivery_number = d.delivery_number AND x.player_id = d.striker_id AND x.is_out
                WHERE d.phase = ?
                GROUP BY d.striker_id"""
            params = [*params, phase]
        else:
            source = f"""
                SELECT player_id, count(DISTINCT match_id) AS matches, count(*) AS innings,
                       sum(runs) AS runs, sum(balls) AS balls, sum(fours) AS fours, sum(sixes) AS sixes,
                       count(*) FILTER (WHERE is_out) AS outs,
                       count(*) FILTER (WHERE runs >= 100) AS hundreds,
                       count(*) FILTER (WHERE runs BETWEEN 50 AND 99) AS fifties
                FROM batting_innings WHERE player_id IS NOT NULL{where}
                GROUP BY player_id"""
        value = {
            "runs": "runs", "sixes": "sixes", "fours": "fours", "hundreds": "hundreds", "fifties": "fifties",
            "average": "runs / nullif(outs, 0)", "strike_rate": "100.0 * runs / nullif(balls, 0)",
        }[stat]
        cols = "matches, innings, runs, balls, outs, fours, sixes" + (", hundreds, fifties" if not phase else "")
    else:
        where, params = filters.where()
        if phase:
            sub, params = _filtered("deliveries", filters, "batting_team")
            source = f"""
                SELECT d.bowler_id AS player_id, count(DISTINCT d.match_id) AS matches,
                       count(DISTINCT d.match_id || '-' || d.innings) AS innings,
                       count(*) FILTER (WHERE d.is_legal_delivery) AS balls, sum(d.bowler_runs) AS runs,
                       count(*) FILTER (WHERE d.is_legal_delivery AND d.bowler_runs = 0) AS dots,
                       count(x.player_id) AS wickets
                FROM {sub} d
                LEFT JOIN dismissals x ON x.match_id = d.match_id AND x.innings = d.innings
                     AND x.delivery_number = d.delivery_number AND x.bowler_credited
                WHERE d.phase = ?
                GROUP BY d.bowler_id"""
            params = [*params, phase]
        else:
            source = f"""
                SELECT player_id, count(DISTINCT match_id) AS matches, count(*) AS innings,
                       sum(balls) AS balls, sum(runs) AS runs, sum(dots) AS dots, sum(wickets) AS wickets
                FROM bowling_innings WHERE player_id IS NOT NULL{where}
                GROUP BY player_id"""
        value = {
            "wickets": "wickets", "economy": "6.0 * runs / nullif(balls, 0)",
            "bowling_average": "runs / nullif(wickets, 0)", "bowling_strike_rate": "balls / nullif(wickets, 0)",
            "dot_ball_pct": "100.0 * dots / nullif(balls, 0)",
        }[stat]
        cols = "matches, innings, balls, runs, wickets, dots"

    order = "ASC" if stat in _LOWER_IS_BETTER else "DESC"
    rows = _rows(
        con,
        f"""SELECT p.name AS player, p.country, {cols}, round({value}, 2) AS value
            FROM ({source}) s JOIN players p USING (player_id)
            WHERE balls >= ? AND {value} IS NOT NULL{"" if is_rate else f" AND {value} > 0"}
            ORDER BY value {order}, balls DESC LIMIT ?""",
        [*params, min_balls, limit],
    )
    for r in rows:
        r[stat] = r.pop("value")
        # Give every common ratio, so the model never has to work one out itself.
        if batting:
            r["average"] = _ratio(r["runs"], r["outs"])
            r["strike_rate"] = _ratio(100 * r["runs"], r["balls"])
            r["boundary_pct"] = _ratio(100 * (r["fours"] + r["sixes"]), r["balls"], 1)
        else:
            r["overs"] = _overs(r["balls"])
            r["economy"] = _ratio(6 * r["runs"], r["balls"])
            r["bowling_average"] = _ratio(r["runs"], r["wickets"])
            r["bowling_strike_rate"] = _ratio(r["balls"], r["wickets"], 1)
            r["dot_ball_pct"] = _ratio(100 * r["dots"], r["balls"], 1)
    return {
        "stat": stat,
        "phase": phase,
        "filters": filters.describe(),
        "minimum_balls": min_balls,
        "leaders": rows,
        "coverage": _match_coverage(con, filters),
    }


# --- Phases ------------------------------------------------------------------


def _phase_rows(con, by_col: str, value_sql: str, batting: bool, filters: Filters, value) -> list[dict]:
    """Per-phase figures for deliveries where `by_col` matches (a player id or a team's names)."""
    sub, params = _filtered("deliveries", filters, "bowling_team" if batting else "batting_team")
    if batting:
        sql = f"""
            SELECT d.phase, count(DISTINCT d.match_id || '-' || d.innings) AS innings,
                   count(*) FILTER (WHERE d.faced) AS balls, sum(d.runs_off_bat) AS runs,
                   count(*) FILTER (WHERE d.faced AND d.runs_off_bat = 0) AS dots,
                   count(*) FILTER (WHERE d.is_four) AS fours, count(*) FILTER (WHERE d.is_six) AS sixes,
                   count(x.player_id) AS outs
            FROM {sub} d
            LEFT JOIN dismissals x ON x.match_id = d.match_id AND x.innings = d.innings
                 AND x.delivery_number = d.delivery_number AND x.is_out AND x.player_id = d.striker_id
            WHERE d.phase IS NOT NULL AND d.{by_col} {value_sql}
            GROUP BY d.phase"""
    else:
        sql = f"""
            SELECT d.phase, count(DISTINCT d.match_id || '-' || d.innings) AS innings,
                   count(*) FILTER (WHERE d.is_legal_delivery) AS balls, sum(d.bowler_runs) AS runs,
                   count(*) FILTER (WHERE d.is_legal_delivery AND d.bowler_runs = 0) AS dots,
                   count(*) FILTER (WHERE d.is_four) AS fours, count(*) FILTER (WHERE d.is_six) AS sixes,
                   count(x.player_id) AS wickets
            FROM {sub} d
            LEFT JOIN dismissals x ON x.match_id = d.match_id AND x.innings = d.innings
                 AND x.delivery_number = d.delivery_number AND x.bowler_credited
            WHERE d.phase IS NOT NULL AND d.{by_col} {value_sql}
            GROUP BY d.phase"""
    rows = {r["phase"]: r for r in _rows(con, sql, [*params, value])}
    out = []
    for phase in PHASES:
        r = rows.get(phase)
        if not r:
            continue
        r["dot_ball_pct"] = _ratio(100 * r["dots"], r["balls"], 1)
        r["boundary_pct"] = _ratio(100 * (r["fours"] + r["sixes"]), r["balls"], 1)
        if batting:
            r["strike_rate"] = _ratio(100 * r["runs"], r["balls"])
            r["average"] = _ratio(r["runs"], r["outs"])
        else:
            r["overs"] = _overs(r["balls"])
            r["economy"] = _ratio(6 * r["runs"], r["balls"])
            r["average"] = _ratio(r["runs"], r["wickets"])
            r["strike_rate"] = _ratio(r["balls"], r["wickets"], 1)
        out.append(r)
    return out


_PHASE_NOTE = (
    "Phases: T20 powerplay overs 1-6, middle 7-15, death 16-20; ODI 1-10, 11-40, 41-50; "
    "The Hundred: first 25 balls, middle 50, last 25. Tests have no phases."
)


def player_phase_stats(con, player: str, role: str = "batting", filters: Filters | None = None) -> dict:
    """A player's batting or bowling in the powerplay, middle and death overs (limited overs only)."""
    filters = _filters(con, filters)
    if role not in ("batting", "bowling"):
        raise ValueError("role must be batting or bowling")
    p = resolve_player(con, player)
    col = "striker_id" if role == "batting" else "bowler_id"
    rows = _phase_rows(con, col, "= ?", role == "batting", filters, p["player_id"])
    return {"player": p, "role": role, "filters": filters.describe(), "by_phase": rows,
            "phase_note": _PHASE_NOTE, "note": COVERAGE_NOTE}


def team_phase_stats(con, team: str, role: str = "batting", filters: Filters | None = None) -> dict:
    """A team's batting or bowling by phase. Runs here are off the bat for batting
    and charged to bowlers for bowling, so extras like byes are left out."""
    filters = _team_filters(con, filters)
    if role not in ("batting", "bowling"):
        raise ValueError("role must be batting or bowling")
    t = resolve_team(con, team)
    col = "batting_team" if role == "batting" else "bowling_team"
    rows = _phase_rows(con, col, f"IN {_TEAM_NAMES_SQL}", role == "batting", filters, t)
    for r in rows:
        r["runs_per_innings"] = _ratio(r["runs"], r["innings"], 1)
        key = "outs" if role == "batting" else "wickets"
        r["wickets_per_innings"] = _ratio(r[key], r["innings"], 2)
    return {"team": t, "role": role, "filters": filters.describe(), "by_phase": rows,
            "phase_note": _PHASE_NOTE, "note": COVERAGE_NOTE}


# --- Line, length and shots --------------------------------------------------

_BREAKDOWNS = {
    "length": ("ball_length", """CASE ball_length WHEN 'yorker' THEN 1 WHEN 'full toss' THEN 2 WHEN 'full' THEN 3
        WHEN 'good length' THEN 4 WHEN 'short of a good length' THEN 5 WHEN 'short' THEN 6 END"""),
    "line": ("ball_line", """CASE ball_line WHEN 'wide outside offstump' THEN 1 WHEN 'outside offstump' THEN 2
        WHEN 'on the stumps' THEN 3 WHEN 'down leg' THEN 4 WHEN 'wide down leg' THEN 5 END"""),
    "shot": ("shot_group", "balls DESC"),
    "shot_detail": ("shot_played", "balls DESC"),
}


def delivery_breakdown(con, player: str, by: str = "length", role: str = "batting", filters: Filters | None = None) -> dict:
    """A player's record split by the line or length of the ball, or by the shot played,
    from Cricinfo's ball tracking. `by` is length, line, shot or shot_detail.

    Dismissals here are those credited to the bowler. Not every ball has tracking
    data; the coverage block says how many of the player's balls do.
    """
    filters = _filters(con, filters)
    if by not in _BREAKDOWNS:
        raise ValueError(f"by must be one of {', '.join(_BREAKDOWNS)}")
    if role not in ("batting", "bowling"):
        raise ValueError("role must be batting or bowling")
    p = resolve_player(con, player)
    col, order = _BREAKDOWNS[by]
    batting = role == "batting"
    who = "striker_id" if batting else "bowler_id"
    opp = "bowling_team" if batting else "batting_team"
    sub, params = _filtered("tracked_deliveries", filters, opp)
    runs = "d.runs_off_bat" if batting else "d.bowler_runs"
    balls = "d.faced" if batting else "d.is_legal_delivery"
    rows = _rows(
        con,
        f"""SELECT d.{col} AS value,
                   count(*) FILTER (WHERE {balls}) AS balls,
                   sum({runs}) AS runs,
                   count(*) FILTER (WHERE {balls} AND {runs} = 0) AS dots,
                   count(*) FILTER (WHERE d.is_four OR d.is_six) AS boundaries,
                   count(x.player_id) AS dismissals,
                   count(*) FILTER (WHERE d.shot_control = 'in control') AS in_control,
                   count(d.shot_control) AS control_known
            FROM {sub} d
            LEFT JOIN dismissals x ON x.match_id = d.match_id AND x.innings = d.innings
                 AND x.delivery_number = d.delivery_number AND x.bowler_credited
            WHERE d.{who} = ? AND d.{col} IS NOT NULL
            GROUP BY d.{col} ORDER BY {order}""",
        [*params, p["player_id"]],
    )
    tracked = sum(r["balls"] for r in rows)
    for r in rows:
        r[by] = r.pop("value")
        r["share_pct"] = _ratio(100 * r["balls"], tracked, 1)
        r["dot_ball_pct"] = _ratio(100 * r["dots"], r["balls"], 1)
        r["in_control_pct"] = _ratio(100 * r.pop("in_control"), r.pop("control_known"), 1)
        r["average"] = _ratio(r["runs"], r["dismissals"])
        if batting:
            r["strike_rate"] = _ratio(100 * r["runs"], r["balls"])
        else:
            r["economy"] = _ratio(6 * r["runs"], r["balls"])
    sub, params = _filtered("deliveries", filters, opp)
    total = con.execute(
        f"""SELECT count(*) FILTER (WHERE {balls}), count(DISTINCT d.match_id), min(d.start_date), max(d.start_date)
            FROM {sub} d WHERE d.{who} = ?""",
        [*params, p["player_id"]],
    ).fetchone()
    coverage = _coverage(total[1], total[2], total[3])
    coverage["balls_with_this_data"] = tracked
    coverage["balls_in_total"] = total[0]
    coverage["tracking_note"] = (
        "Line, length and shot come from ESPNcricinfo's ball tracking, which covers some matches only "
        "(line and length mostly from 2014 on)."
    )
    return {"player": p, "role": role, "by": by, "filters": filters.describe(), "breakdown": rows, "coverage": coverage}


# --- Over time ---------------------------------------------------------------


def player_by_year(con, player: str, role: str = "batting", filters: Filters | None = None,
                   by_format: bool = False, min_matches: int = 3) -> dict:
    """A player's record year by year, for questions about form and consistency.

    The `consistency` block compares the years with at least `min_matches`
    matches: the best and worst year by average, and how far the yearly
    averages spread (the lower the spread, the more consistent).
    """
    filters = _filters(con, filters)
    if role not in ("batting", "bowling"):
        raise ValueError("role must be batting or bowling")
    p = resolve_player(con, player)
    where, params = filters.where()
    group = "year(start_date), format" if by_format else "year(start_date)"
    fmt = "format" if by_format else "NULL"
    if role == "batting":
        rows = _rows(
            con,
            f"""SELECT year(start_date) AS year, {fmt} AS format, count(DISTINCT match_id) AS matches,
                       count(*) AS innings, sum(runs) AS runs, sum(balls) AS balls,
                       count(*) FILTER (WHERE is_out) AS outs,
                       count(*) FILTER (WHERE runs >= 100) AS hundreds,
                       count(*) FILTER (WHERE runs BETWEEN 50 AND 99) AS fifties
                FROM batting_innings WHERE player_id = ?{where}
                GROUP BY {group} ORDER BY {group}""",
            [p["player_id"], *params],
        )
        for r in rows:
            r["average"] = _ratio(r["runs"], r["outs"])
            r["strike_rate"] = _ratio(100 * r["runs"], r["balls"])
    else:
        rows = _rows(
            con,
            f"""SELECT year(start_date) AS year, {fmt} AS format, count(DISTINCT match_id) AS matches,
                       sum(balls) AS balls, sum(runs) AS runs, sum(wickets) AS wickets,
                       count(*) FILTER (WHERE wickets >= 5) AS five_wickets
                FROM bowling_innings WHERE player_id = ?{where}
                GROUP BY {group} ORDER BY {group}""",
            [p["player_id"], *params],
        )
        for r in rows:
            r["average"] = _ratio(r["runs"], r["wickets"])
            r["economy"] = _ratio(6 * r["runs"], r["balls"])
            r["strike_rate"] = _ratio(r["balls"], r["wickets"], 1)
    totals_by_format = None
    if by_format:
        totals_by_format = {}
        for r in rows:
            t = totals_by_format.setdefault(r["format"], {"years": 0, "matches": 0, "balls": 0, "runs": 0, "outs": 0, "wickets": 0})
            t["years"] += 1
            for k in ("matches", "balls", "runs", "outs", "wickets"):
                t[k] += r.get(k) or 0
        for t in totals_by_format.values():
            if role == "batting":
                del t["wickets"]
                t["average"], t["strike_rate"] = _ratio(t["runs"], t["outs"]), _ratio(100 * t["runs"], t["balls"])
            else:
                del t["outs"]
                t["average"], t["economy"] = _ratio(t["runs"], t["wickets"]), _ratio(6 * t["runs"], t["balls"])
                t["strike_rate"] = _ratio(t["balls"], t["wickets"], 1)
    else:
        for r in rows:
            r.pop("format")

    span = con.execute(
        f"SELECT count(*), min(start_date), max(start_date) FROM player_matches WHERE player_id = ?{where}",
        [p["player_id"], *params],
    ).fetchone()
    result = {"player": p, "role": role, "filters": filters.describe(), "by_year": rows, "coverage": _coverage(*span)}
    if totals_by_format is None:
        result["consistency"] = _consistency(rows, role, min_matches)
    else:
        # Years are only comparable within a format, so each format gets its own summary.
        for fmt, totals in totals_by_format.items():
            totals["consistency"] = _consistency([r for r in rows if r["format"] == fmt], role, min_matches)
        result["totals_by_format"] = totals_by_format
    return result


def _consistency(rows: list[dict], role: str, min_matches: int) -> dict | None:
    """Best and worst year, median and spread of the yearly averages, over years
    with at least `min_matches` matches. None if fewer than two such years."""
    counted = [r for r in rows if r["matches"] >= min_matches and r["average"] is not None]
    if len(counted) < 2:
        return None
    avgs = sorted(r["average"] for r in counted)
    mean = sum(avgs) / len(avgs)
    spread = (sum((a - mean) ** 2 for a in avgs) / len(avgs)) ** 0.5
    # For batting a higher average is better; for bowling a lower one.
    best = max(counted, key=lambda r: r["average"]) if role == "batting" else min(counted, key=lambda r: r["average"])
    worst = min(counted, key=lambda r: r["average"]) if role == "batting" else max(counted, key=lambda r: r["average"])
    mid = len(avgs) // 2
    return {
        "years_counted": len(counted),
        "min_matches_per_year": min_matches,
        "best_year": {"year": best["year"], "average": best["average"]},
        "worst_year": {"year": worst["year"], "average": worst["average"]},
        "median_yearly_average": avgs[mid] if len(avgs) % 2 else round((avgs[mid - 1] + avgs[mid]) / 2, 2),
        "spread_of_yearly_averages": round(spread, 2),
        "spread_pct_of_mean": round(100 * spread / mean, 1) if mean else None,
        "how_to_read": "spread is the standard deviation of the yearly averages; a smaller spread means steadier year-to-year output",
    }


# --- Scorecards --------------------------------------------------------------


def match_scorecard(con, match_id: int) -> dict:
    """The full scorecard of one match: result, and every batter's and bowler's
    figures in each innings (super overs left out). Get match ids from
    team_head_to_head or best_innings."""
    info = _rows(
        con,
        """SELECT match_id, start_date AS date, format, gender, event, event_stage, venue, city,
                  team_1, team_2, toss_winner, toss_decision, player_of_match, winner, outcome, eliminator
           FROM matches WHERE match_id = ?""",
        [match_id],
    )
    if not info:
        raise LookupError(f"No match with id {match_id} in the data")
    match = info[0]
    match["date"] = str(match["date"])
    result = _rows(con, "SELECT team, result, margin, won_tiebreak FROM match_results WHERE match_id = ?", [match_id])
    winner = next((r for r in result if r["result"] == "won"), None)
    tiebreak = next((r for r in result if r["won_tiebreak"]), None)
    match["result"] = (
        f"{winner['team']} won by {winner['margin']}" if winner and winner["margin"] else
        f"{winner['team']} won" if winner else
        f"tied ({tiebreak['team']} won the super over)" if tiebreak else
        result[0]["result"] if result else None
    )
    # How each batter was out, scorecard style, with the fielders from the data.
    dismissal_rows = con.execute(
        """SELECT w.innings, w.player_dismissed_id, w.wicket_type, d.bowler,
                  list(CASE WHEN f.substitute THEN 'sub (' || f.fielder || ')' ELSE f.fielder END
                       ORDER BY f.fielder_number) FILTER (WHERE f.fielder IS NOT NULL) AS fielders
           FROM wickets w
           JOIN ball_by_ball_data d USING (match_id, innings, delivery_number)
           LEFT JOIN wicket_fielders f USING (match_id, innings, delivery_number, wicket_number)
           WHERE w.match_id = ?
           GROUP BY w.innings, w.delivery_number, w.wicket_number, w.player_dismissed_id, w.wicket_type, d.bowler
           ORDER BY w.innings, w.delivery_number, w.wicket_number""",
        [match_id],
    ).fetchall()
    dismissal_text = {}
    for inns, player_id, kind, bowler, fielders in dismissal_rows:
        fielders = fielders or []
        catcher = fielders[0] if fielders else None
        text = {
            "bowled": f"b {bowler}",
            "lbw": f"lbw b {bowler}",
            "caught and bowled": f"c & b {bowler}",
            "caught": (f"c & b {bowler}" if catcher == bowler else f"c {catcher} b {bowler}") if catcher else f"c ? b {bowler}",
            "stumped": f"st {catcher} b {bowler}" if catcher else f"st b {bowler}",
            "hit wicket": f"hit wicket b {bowler}",
            "run out": f"run out ({'/'.join(fielders)})" if fielders else "run out",
        }.get(kind, kind)
        dismissal_text[(inns, player_id)] = text  # the last one wins (retired hurt, then out)

    innings = []
    for t in _rows(
        con,
        """SELECT innings, team, opposition, runs, wickets, declared, balls, balls_per_over, format, extras, target_runs
           FROM team_innings WHERE match_id = ? ORDER BY innings""",
        [match_id],
    ):
        batting = _rows(
            con,
            """SELECT position, player, player_id, runs, balls, fours, sixes, is_out
               FROM batting_innings WHERE match_id = ? AND innings = ? ORDER BY position""",
            [match_id, t["innings"]],
        )
        for b in batting:
            out = b.pop("is_out")
            b["score"] = f"{b['runs']}{'' if out else '*'} ({b['balls']})"
            b["dismissal"] = dismissal_text.get((t["innings"], b.pop("player_id"))) if out else "not out"
        # Bowlers in the order they came on, as a scorecard lists them.
        bowling = _rows(
            con,
            """SELECT b.player, b.balls, b.balls_per_over, b.maidens, b.runs, b.wickets, b.wides, b.noballs
               FROM bowling_innings b
               JOIN (SELECT bowler_id, min(delivery_number) AS first_ball FROM deliveries
                     WHERE match_id = ? AND innings = ? GROUP BY bowler_id) f
                 ON f.bowler_id IS NOT DISTINCT FROM b.player_id
               WHERE b.match_id = ? AND b.innings = ? ORDER BY f.first_ball""",
            [match_id, t["innings"], match_id, t["innings"]],
        )
        for b in bowling:
            b["figures"] = f"{_overs(b['balls'], b.pop('balls_per_over'))}-{b['maidens']}-{b['runs']}-{b['wickets']}"
            b["economy"] = _ratio(6 * b["runs"], b["balls"])
        innings.append({
            "innings": t["innings"],
            "team": t["team"],
            "total": _score(t["runs"], t["wickets"], t["declared"], t["balls"], t["balls_per_over"], t["format"]),
            "extras": t["extras"],
            "target": t["target_runs"],
            "batting": batting,
            "bowling": bowling,
        })
    return {"match": match, "innings": innings, "note": COVERAGE_NOTE}
