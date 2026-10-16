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

BOWLER_WICKETS = ("bowled", "caught", "caught and bowled", "lbw", "stumped", "hit wicket")
NOT_OUT_RETIREMENTS = ("retired hurt", "retired not out")


def _sql_list(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


# Bowling styles grouped the way journalists ask about them. A player with
# several styles listed is grouped by the first, which is their main one.
_MAIN_STYLE = "trim(split_part(style, ',', 1))"

DERIVED = {
    # name: (kind, sql). Views are cheap to define and stay in sync with the
    # raw tables; tables are for the aggregates that are expensive to compute.
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
    "deliveries": ("VIEW", """
        SELECT b.*,
               m.venue, m.city, m.collection, m.balls_per_over,
               b.wides IS NULL AS faced,
               b.runs_off_bat + coalesce(b.wides, 0) + coalesce(b.noballs, 0) AS bowler_runs,
               b.runs_off_bat = 4 AND NOT b.non_boundary AS is_four,
               b.runs_off_bat = 6 AND NOT b.non_boundary AS is_six,
               st.arm AS bowler_arm, st.kind AS bowler_kind
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
    for name, (kind, sql) in DERIVED.items():
        con.execute(f'DROP VIEW IF EXISTS "{name}"')
        con.execute(f'DROP TABLE IF EXISTS "{name}"')
        con.execute(f'CREATE {kind} "{name}" AS {sql}')
