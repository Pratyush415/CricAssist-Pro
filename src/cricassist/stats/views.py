"""Derived tables built on top of the raw dataset tables.

The scoring rules follow ESPNcricinfo's conventions:

- Super over innings are left out of every player stat.
- A batter's balls faced count no balls but not wides.
- Retired hurt and retired not out are not out; retired out is out.
- A bowler is credited with bowled, caught, caught and bowled, lbw, stumped
  and hit wicket. Run outs and the rare non-bowling dismissals are not.
- Runs conceded by a bowler are runs off the bat plus wides and no balls.
  Byes, leg byes and penalty runs are not charged to the bowler.
"""

from cricassist.stats.teams import alias_rows

BOWLER_WICKETS = ("bowled", "caught", "caught and bowled", "lbw", "stumped", "hit wicket")
NOT_OUT_RETIREMENTS = ("retired hurt", "retired not out")


def _sql_list(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_ALIASES = ", ".join(f"({_sql_str(name)}, {_sql_str(team)})" for name, team in alias_rows())

# Phases of a limited overs innings, by over (counted from 0). T20: powerplay
# overs 1-6, middle 7-15, death 16-20. ODI: 1-10, 11-40, 41-50. The Hundred
# (sets of 5 balls): powerplay the first 25 balls, death the last 25.
_PHASE = """
    CASE
        WHEN b.format = 'T20' AND m.collection = 'hnd' THEN
            CASE WHEN b.over < 5 THEN 'powerplay' WHEN b.over < 15 THEN 'middle' ELSE 'death' END
        WHEN b.format = 'T20' THEN
            CASE WHEN b.over < 6 THEN 'powerplay' WHEN b.over < 15 THEN 'middle' ELSE 'death' END
        WHEN b.format = 'ODI' THEN
            CASE WHEN b.over < 10 THEN 'powerplay' WHEN b.over < 40 THEN 'middle' ELSE 'death' END
    END"""

# The 41 shot names in the tracking data, grouped into families.
_SHOT_GROUP = """
    CASE
        WHEN t.shot_played IS NULL THEN NULL
        WHEN t.shot_played IN ('defended', 'forward defence', 'back defence', 'padded away') THEN 'defence'
        WHEN t.shot_played IN ('left alone', 'no shot') THEN 'leave / no shot'
        WHEN t.shot_played LIKE '%drive%' OR t.shot_played = 'vertical forward attack' THEN 'drive'
        WHEN t.shot_played LIKE '%cut%' THEN 'cut'
        WHEN t.shot_played LIKE 'pull%' OR t.shot_played = 'hook' THEN 'pull / hook'
        WHEN t.shot_played = 'slog sweep' THEN 'slog sweep'
        WHEN t.shot_played IN ('reverse sweep', 'reverse scoop') THEN 'reverse sweep / scoop'
        WHEN t.shot_played LIKE '%sweep%' OR t.shot_played = 'paddle away' THEN 'sweep'
        WHEN t.shot_played = 'ramp' THEN 'ramp'
        WHEN t.shot_played = 'slog shot' THEN 'slog'
        WHEN t.shot_played IN ('flick', 'leg glance') THEN 'flick / glance'
        WHEN t.shot_played IN ('push', 'push shot', 'dab', 'steered') THEN 'push / dab / steer'
        ELSE 'other'
    END"""

# Bowling styles grouped the way journalists ask about them. A player with
# several styles listed is grouped by the first, which is their main one.
_MAIN_STYLE = "trim(split_part(style, ',', 1))"

DERIVED = {
    # name: (kind, sql). Views are cheap to define and stay in sync with the
    # raw tables; tables are for the aggregates that are expensive to compute.

    # Every team name, earlier franchise name and short name, with the
    # team's current name (see teams.py).
    "team_names": ("TABLE", f"""
        WITH aliases(name, team) AS (VALUES {_ALIASES}),
        names AS (SELECT team_1 AS name FROM matches UNION SELECT team_2 FROM matches)
        SELECT n.name, coalesce(a.team, n.name) AS team
        FROM names n LEFT JOIN aliases a USING (name)
        UNION
        SELECT name, team FROM aliases
    """),
    "bowling_styles": ("TABLE", f"""
        SELECT style,
               CASE WHEN main LIKE 'left-arm%' OR main = 'slow left-arm orthodox'
                    THEN 'left-arm' ELSE 'right-arm' END AS arm,
               CASE WHEN main = 'right-arm offbreak' THEN 'off-spin'
                    WHEN main IN ('legbreak', 'legbreak googly') THEN 'leg-spin'
                    WHEN main = 'slow left-arm orthodox' THEN 'left-arm orthodox'
                    WHEN main = 'left-arm wrist-spin' THEN 'left-arm wrist-spin'
                    WHEN main LIKE '%fast%' OR main LIKE '%medium%' THEN 'pace'
                    ELSE 'other' END AS kind
        FROM (
            SELECT style, {_MAIN_STYLE} AS main
            FROM (
                SELECT bowling_style AS style FROM players
                UNION SELECT bowling_style_bowler FROM ball_by_ball_data
            )
            WHERE style IS NOT NULL
        )
    """),
    # Every ball outside super overs, with the match details and the runs
    # charged to the bowler.
    "deliveries": ("VIEW", f"""
        SELECT b.*,
               m.venue, m.city, m.collection, m.balls_per_over,
               b.wides IS NULL AS faced,
               b.runs_off_bat + coalesce(b.wides, 0) + coalesce(b.noballs, 0) AS bowler_runs,
               b.runs_off_bat = 4 AND NOT b.non_boundary AS is_four,
               b.runs_off_bat = 6 AND NOT b.non_boundary AS is_six,
               st.arm AS bowler_arm, st.kind AS bowler_kind,
               {_PHASE} AS phase
        FROM ball_by_ball_data b
        JOIN innings i USING (match_id, innings)
        JOIN matches m USING (match_id)
        LEFT JOIN bowling_styles st ON st.style = b.bowling_style_bowler
        WHERE NOT i.is_super_over
    """),
    # Every wicket outside super overs, with the bowler of that ball and
    # whether the bowler is credited with it.
    "dismissals": ("VIEW", f"""
        SELECT w.match_id, w.innings, w.delivery_number, w.wicket_number,
               w.player_dismissed AS player, w.player_dismissed_id AS player_id, w.wicket_type,
               d.bowler, d.bowler_id, d.batting_team, d.bowling_team,
               w.wicket_type IN {_sql_list(BOWLER_WICKETS)} AS bowler_credited,
               w.wicket_type NOT IN {_sql_list(NOT_OUT_RETIREMENTS)} AS is_out
        FROM wickets w
        JOIN deliveries d USING (match_id, innings, delivery_number)
    """),
    # Deliveries with Cricinfo's line, length and shot data (about half of
    # matches since 2014 have line and length; most have the shot).
    "tracked_deliveries": ("VIEW", f"""
        SELECT d.*, t.ball_line, t.ball_length, t.shot_played, t.shot_control,
               t.wagon_zone, t.shot_direction, {_SHOT_GROUP} AS shot_group
        FROM deliveries d
        JOIN cricinfo_tracking t USING (match_id, innings, delivery_number)
    """),
    # One row per team per match, under the team's current name, with the result.
    "match_results": ("VIEW", """
        WITH sides AS (
            SELECT m.*, m.team_1 AS side, m.team_2 AS other FROM matches m
            UNION ALL
            SELECT m.*, m.team_2, m.team_1 FROM matches m
        )
        SELECT s.match_id, s.start_date, s.season, s.gender, s.format, s.type, s.event,
               s.venue, s.city, s.collection,
               t.team, o.team AS opposition, s.side AS team_as_named,
               CASE
                   WHEN s.winner IS NOT NULL AND s.winner = s.side THEN 'won'
                   WHEN s.winner IS NOT NULL THEN 'lost'
                   WHEN s.outcome = 'tie' THEN 'tied'
                   WHEN s.outcome = 'draw' THEN 'drawn'
                   ELSE 'no result'
               END AS result,
               coalesce(s.outcome = 'tie' AND s.eliminator = s.side, false) AS won_tiebreak,
               CASE
                   WHEN s.won_by_innings THEN 'an innings and ' || s.winner_runs || ' runs'
                   WHEN s.winner_runs IS NOT NULL THEN s.winner_runs || ' runs'
                   WHEN s.winner_wickets IS NOT NULL THEN s.winner_wickets || ' wickets'
               END
               || CASE WHEN s.result_method = 'D/L' THEN ' (D/L)' ELSE '' END AS margin,
               s.toss_winner = s.side AS won_toss, s.toss_decision,
               s.player_of_match
        FROM sides s
        JOIN team_names t ON t.name = s.side
        JOIN team_names o ON o.name = s.other
    """),
    # One row per team innings outside super overs: the total, wickets and
    # overs, under the team's current name.
    "team_innings": ("TABLE", """
        WITH balls AS (
            SELECT match_id, innings,
                   sum(runs_off_bat + extras) AS runs,
                   count(*) FILTER (WHERE is_legal_delivery) AS balls,
                   count(*) FILTER (WHERE is_four) AS fours,
                   count(*) FILTER (WHERE is_six) AS sixes,
                   sum(extras) AS extras
            FROM deliveries GROUP BY match_id, innings
        ),
        outs AS (
            SELECT match_id, innings, count(*) FILTER (WHERE is_out) AS wickets
            FROM dismissals GROUP BY match_id, innings
        )
        SELECT i.match_id, i.innings,
               t.team, o.team AS opposition, i.batting_team AS team_as_named,
               coalesce(b.runs, 0) + i.penalty_runs_pre + i.penalty_runs_post AS runs,
               coalesce(w.wickets, 0) AS wickets,
               coalesce(b.balls, 0) AS balls, m.balls_per_over,
               coalesce(b.fours, 0) AS fours, coalesce(b.sixes, 0) AS sixes,
               coalesce(b.extras, 0) AS extras,
               i.declared, i.target_runs, i.target_overs, m.scheduled_overs,
               i.innings = max(i.innings) OVER (PARTITION BY i.match_id) AS is_last_innings,
               r.result, r.won_tiebreak, r.margin,
               m.start_date, m.season, m.gender, m.format, m.type, m.event, m.venue, m.city, m.collection
        FROM innings i
        JOIN matches m USING (match_id)
        JOIN team_names t ON t.name = i.batting_team
        JOIN team_names o ON o.name = CASE WHEN i.batting_team = m.team_1 THEN m.team_2 ELSE m.team_1 END
        JOIN match_results r ON r.match_id = i.match_id AND r.team_as_named = i.batting_team
        LEFT JOIN balls b ON b.match_id = i.match_id AND b.innings = i.innings
        LEFT JOIN outs w ON w.match_id = i.match_id AND w.innings = i.innings
        WHERE NOT i.is_super_over
    """),
    # One row per player per match they were in the XI for (or a substitute
    # who came in), whether or not they batted or bowled.
    "player_matches": ("VIEW", """
        SELECT p.match_id, p.person_id AS player_id, p.person AS player, p.team,
               CASE WHEN p.team = m.team_1 THEN m.team_2 ELSE m.team_1 END AS opposition,
               m.start_date, m.season, m.gender, m.format, m.type, m.event, m.venue, m.city, m.collection
        FROM match_people p
        JOIN matches m USING (match_id)
        WHERE p.role = 'player'
    """),
    # One row per batter per innings, including batters who never faced a
    # ball (a non striker left not out, or run out without facing).
    "batting_innings": ("TABLE", """
        WITH appearances AS (
            SELECT match_id, innings, striker_id AS player_id, striker AS player, delivery_number * 2 AS seq
            FROM deliveries
            UNION ALL
            SELECT d.match_id, d.innings, p.person_id, d.non_striker, d.delivery_number * 2 + 1
            FROM deliveries d
            LEFT JOIN match_people p
              ON p.match_id = d.match_id AND p.role = 'player'
             AND p.team = d.batting_team AND p.person = d.non_striker
            UNION ALL
            SELECT match_id, innings, player_id, player, delivery_number * 2 + 1
            FROM dismissals
        ),
        batters AS (
            SELECT match_id, innings, player_id, any_value(player) AS player, min(seq) AS seq
            FROM appearances
            GROUP BY match_id, innings, player_id
        ),
        scoring AS (
            SELECT match_id, innings, striker_id AS player_id,
                   sum(runs_off_bat) AS runs,
                   count(*) FILTER (WHERE faced) AS balls,
                   count(*) FILTER (WHERE faced AND runs_off_bat = 0) AS dots,
                   count(*) FILTER (WHERE is_four) AS fours,
                   count(*) FILTER (WHERE is_six) AS sixes
            FROM deliveries
            GROUP BY match_id, innings, striker_id
        ),
        last_wicket AS (
            -- A batter who retired hurt can come back, so the last wicket decides.
            SELECT match_id, innings, player_id,
                   arg_max(is_out, delivery_number * 100 + wicket_number) AS is_out,
                   arg_max(wicket_type, delivery_number * 100 + wicket_number) AS how_out,
                   arg_max(CASE WHEN bowler_credited THEN bowler END, delivery_number * 100 + wicket_number) AS dismissed_by,
                   arg_max(CASE WHEN bowler_credited THEN bowler_id END, delivery_number * 100 + wicket_number) AS dismissed_by_id
            FROM dismissals
            GROUP BY match_id, innings, player_id
        )
        SELECT b.match_id, b.innings, b.player_id, b.player,
               row_number() OVER (PARTITION BY b.match_id, b.innings ORDER BY b.seq) AS position,
               i.batting_team AS team,
               CASE WHEN i.batting_team = m.team_1 THEN m.team_2 ELSE m.team_1 END AS opposition,
               coalesce(s.runs, 0) AS runs, coalesce(s.balls, 0) AS balls, coalesce(s.dots, 0) AS dots,
               coalesce(s.fours, 0) AS fours, coalesce(s.sixes, 0) AS sixes,
               coalesce(w.is_out, false) AS is_out,
               coalesce(w.how_out, 'not out') AS how_out,
               w.dismissed_by, w.dismissed_by_id,
               m.start_date, m.season, m.gender, m.format, m.type, m.event, m.venue, m.city, m.collection
        FROM batters b
        JOIN innings i USING (match_id, innings)
        JOIN matches m USING (match_id)
        LEFT JOIN scoring s ON s.match_id = b.match_id AND s.innings = b.innings AND s.player_id IS NOT DISTINCT FROM b.player_id
        LEFT JOIN last_wicket w ON w.match_id = b.match_id AND w.innings = b.innings AND w.player_id IS NOT DISTINCT FROM b.player_id
    """),
    # One row per bowler per innings.
    "bowling_innings": ("TABLE", """
        WITH overs AS (
            -- An over is a maiden when one bowler bowled all of it and
            -- conceded nothing.
            SELECT match_id, innings, over, any_value(bowler_id) AS bowler_id
            FROM deliveries
            GROUP BY match_id, innings, over
            HAVING count(DISTINCT bowler_id) = 1
               AND count(*) FILTER (WHERE is_legal_delivery) >= any_value(balls_per_over)
               AND sum(bowler_runs) = 0
        ),
        maidens AS (
            SELECT match_id, innings, bowler_id, count(*) AS maidens
            FROM overs GROUP BY ALL
        ),
        wickets AS (
            SELECT match_id, innings, bowler_id, count(*) AS wickets
            FROM dismissals WHERE bowler_credited GROUP BY ALL
        ),
        bowling AS (
            SELECT match_id, innings, bowler_id AS player_id, any_value(bowler) AS player,
                   any_value(bowling_team) AS team, any_value(batting_team) AS opposition,
                   any_value(balls_per_over) AS balls_per_over,
                   count(*) FILTER (WHERE is_legal_delivery) AS balls,
                   sum(bowler_runs) AS runs,
                   count(*) FILTER (WHERE is_legal_delivery AND bowler_runs = 0) AS dots,
                   count(*) FILTER (WHERE is_four) AS fours,
                   count(*) FILTER (WHERE is_six) AS sixes,
                   count(*) FILTER (WHERE wides IS NOT NULL) AS wides,
                   count(*) FILTER (WHERE noballs IS NOT NULL) AS noballs
            FROM deliveries
            GROUP BY match_id, innings, bowler_id
        )
        SELECT b.*, coalesce(md.maidens, 0) AS maidens, coalesce(w.wickets, 0) AS wickets,
               m.start_date, m.season, m.gender, m.format, m.type, m.event, m.venue, m.city, m.collection
        FROM bowling b
        JOIN matches m USING (match_id)
        LEFT JOIN maidens md ON md.match_id = b.match_id AND md.innings = b.innings AND md.bowler_id IS NOT DISTINCT FROM b.player_id
        LEFT JOIN wickets w ON w.match_id = b.match_id AND w.innings = b.innings AND w.bowler_id IS NOT DISTINCT FROM b.player_id
    """),
}


def create_derived(con) -> None:
    """(Re)create the derived views and tables, in dependency order."""
    existing = dict(
        con.execute(
            "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    )
    for name, (kind, sql) in DERIVED.items():
        if name in existing:
            con.execute(f'DROP {"VIEW" if existing[name] == "VIEW" else "TABLE"} "{name}"')
        con.execute(f'CREATE {kind} "{name}" AS {sql}')
