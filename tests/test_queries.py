import pytest

from conftest import DC_V_RR, KANPUR_TEST, WI_V_ENG_WOMEN
from cricassist.stats.queries import (
    STYLE_GROUPS,
    Filters,
    PlayerNotFound,
    batter_vs_bowler,
    batter_vs_bowling_style,
    batting_innings_list,
    batting_summary,
    bowling_innings_list,
    bowling_summary,
    find_players,
    resolve_player,
)

ALL_MATCHES = (KANPUR_TEST, DC_V_RR, WI_V_ENG_WOMEN)


# --- The derived tables agree with the raw ball-by-ball totals ---------------


@pytest.mark.parametrize("match_id", ALL_MATCHES)
def test_batting_runs_plus_extras_equal_innings_totals(db, match_id):
    rows = db.execute(
        """SELECT d.innings, sum(d.runs_off_bat + d.extras),
                  (SELECT sum(runs) FROM batting_innings b WHERE b.match_id = d.match_id AND b.innings = d.innings)
                  + sum(d.extras)
           FROM deliveries d WHERE d.match_id = ? GROUP BY d.match_id, d.innings""",
        [match_id],
    ).fetchall()
    assert rows and all(total == from_batters for _, total, from_batters in rows)


@pytest.mark.parametrize("match_id", ALL_MATCHES)
def test_bowling_runs_plus_byes_equal_innings_totals(db, match_id):
    rows = db.execute(
        """SELECT d.innings, sum(d.runs_off_bat + d.extras),
                  (SELECT sum(runs) FROM bowling_innings b WHERE b.match_id = d.match_id AND b.innings = d.innings)
                  + sum(coalesce(d.byes, 0) + coalesce(d.legbyes, 0) + coalesce(d.penalty, 0))
           FROM deliveries d WHERE d.match_id = ? GROUP BY d.match_id, d.innings""",
        [match_id],
    ).fetchall()
    assert rows and all(total == from_bowlers for _, total, from_bowlers in rows)


@pytest.mark.parametrize("match_id", ALL_MATCHES)
def test_wickets_add_up(db, match_id):
    rows = db.execute(
        """SELECT innings, count(*) FILTER (WHERE is_out),
                  count(*) FILTER (WHERE is_out AND NOT bowler_credited),
                  (SELECT coalesce(sum(wickets), 0) FROM bowling_innings b
                   WHERE b.match_id = d.match_id AND b.innings = d.innings)
           FROM dismissals d WHERE match_id = ? GROUP BY match_id, innings""",
        [match_id],
    ).fetchall()
    assert rows and all(outs == others + bowlers for _, outs, others, bowlers in rows)


def test_super_overs_are_left_out(db):
    innings = db.execute(
        "SELECT DISTINCT innings FROM batting_innings WHERE match_id = ? ORDER BY 1", [DC_V_RR]
    ).fetchall()
    assert innings == [(1,), (2,)]
    later = db.execute("SELECT count(*) FROM bowling_innings WHERE match_id = ? AND innings > 2", [DC_V_RR])
    assert later.fetchone() == (0,)


# --- The Kanpur Test scorecard ----------------------------------------------


def test_kanpur_batting_scorecard(db):
    rows = db.execute(
        """SELECT innings, player, runs, balls, fours, sixes, how_out, dismissed_by FROM batting_innings
           WHERE match_id = ? AND runs >= 50 ORDER BY innings, position""",
        [KANPUR_TEST],
    ).fetchall()
    assert rows == [
        (1, "Mominul Haque", 107, 194, 17, 1, "not out", None),
        (2, "YBK Jaiswal", 72, 51, 12, 2, "bowled", "Hasan Mahmud"),
        (2, "KL Rahul", 68, 43, 7, 2, "stumped", "Mehedi Hasan Miraz"),
        (3, "Shadman Islam", 50, 101, 10, 0, "caught", "Akash Deep"),
        (4, "YBK Jaiswal", 51, 45, 8, 1, "caught", "Taijul Islam"),
    ]


def test_declared_innings_lists_only_batters_who_batted(db):
    # India declared at 285/9 the moment Akash Deep was out, so Siraj did not bat.
    batters = db.execute(
        "SELECT player, is_out FROM batting_innings WHERE match_id = ? AND innings = 2 ORDER BY position",
        [KANPUR_TEST],
    ).fetchall()
    assert len(batters) == 10
    assert batters[-1] == ("JJ Bumrah", False)


def test_kanpur_bowling_figures(db):
    rows = db.execute(
        """SELECT innings, balls, maidens, runs, wickets FROM bowling_innings
           WHERE match_id = ? AND player = 'JJ Bumrah' ORDER BY innings""",
        [KANPUR_TEST],
    ).fetchall()
    assert rows == [(1, 108, 7, 50, 3), (3, 60, 5, 17, 3)]


# --- Query functions ---------------------------------------------------------


def test_resolve_player_by_name_id_and_search(db):
    assert resolve_player(db, "Mominul Haque")["player_id"] == resolve_player(db, "mominul")["player_id"]
    pid = resolve_player(db, "JJ Bumrah")["player_id"]
    assert resolve_player(db, pid)["name"] == "JJ Bumrah"
    assert resolve_player(db, "Jasprit Bumrah")["name"] == "JJ Bumrah"


def test_resolve_player_errors(db):
    with pytest.raises(PlayerNotFound) as missing:
        resolve_player(db, "Don Bradman")
    assert missing.value.candidates == []
    with pytest.raises(PlayerNotFound) as ambiguous:
        resolve_player(db, "Hasan")  # Hasan Mahmud, Mehedi Hasan Miraz, Shakib Al Hasan, ...
    assert len(ambiguous.value.candidates) > 1


def test_find_players(db):
    assert [p["name"] for p in find_players(db, "jaiswal")] == ["YBK Jaiswal"]


def test_batting_summary(db):
    s = batting_summary(db, "YBK Jaiswal", Filters(format="TEST"))
    assert (s["matches"], s["innings"], s["not_outs"], s["runs"], s["balls"]) == (1, 2, 0, 123, 96)
    assert (s["highest"], s["fifties"], s["hundreds"], s["average"]) == ("72", 2, 0, 61.5)
    assert s["strike_rate"] == round(100 * 123 / 96, 2)
    assert s["coverage"]["matches_in_data"] == 1


def test_highest_score_marks_not_out(db):
    assert batting_summary(db, "Mominul Haque")["highest"] == "107*"


def test_batting_summary_filters(db):
    assert batting_summary(db, "YBK Jaiswal", Filters(format="T20"))["innings"] == 1
    assert batting_summary(db, "YBK Jaiswal", Filters(competition="ipl", opposition="Delhi Capitals"))["matches"] == 1
    assert batting_summary(db, "YBK Jaiswal", Filters(venue="Kanpur"))["runs"] == 123
    nothing = batting_summary(db, "YBK Jaiswal", Filters(since="2026-01-01"))
    assert (nothing["innings"], nothing["average"], nothing["strike_rate"]) == (0, None, None)


def test_batting_innings_list_newest_first(db):
    form = batting_innings_list(db, "YBK Jaiswal", last=3)
    dates = [i["date"] for i in form["innings"]]
    assert dates == sorted(dates, reverse=True)
    assert form["innings"][-1]["score"] == "72 (51)"


def test_bowling_summary_test_match(db):
    s = bowling_summary(db, "JJ Bumrah", Filters(format="TEST"))
    assert (s["innings"], s["overs"], s["maidens"], s["runs"], s["wickets"]) == (2, "28", 12, 67, 6)
    assert (s["best_innings"], s["best_match"], s["ten_wickets_in_match"]) == ("3/17", "6/67", 0)
    assert s["average"] == round(67 / 6, 2)
    assert s["economy"] == round(6 * 67 / 168, 2)


def test_bowling_innings_list(db):
    form = bowling_innings_list(db, "Mehedi Hasan Miraz", Filters(format="TEST"))
    assert [i["figures"] for i in form["innings"]] == ["9-0-44-2", "6.4-0-41-4"]


def test_batter_vs_bowler(db):
    h2h = batter_vs_bowler(db, "YBK Jaiswal", "Hasan Mahmud")
    assert h2h["dismissals"] == 1
    assert h2h["balls"] > 0


def test_batter_vs_bowling_style(db):
    result = batter_vs_bowling_style(db, "YBK Jaiswal", Filters(format="TEST"))
    by_style = {row["style"]: row for row in result["by_style"]}
    pace, spin = by_style["pace"], by_style["spin"]
    total = batting_summary(db, "YBK Jaiswal", Filters(format="TEST"))
    assert pace["runs"] + spin["runs"] <= total["runs"]  # bowlers with no style on record are left out
    detail = [r for s, r in by_style.items() if s not in ("pace", "spin")]
    assert sum(r["balls"] for r in detail) == pace["balls"] + spin["balls"]


def test_batter_vs_one_style_group(db):
    one = batter_vs_bowling_style(db, "YBK Jaiswal", style="Left-arm pace")
    assert [r["style"] for r in one["by_style"]] == ["left-arm pace"]
    with pytest.raises(ValueError, match="Unknown bowling style"):
        batter_vs_bowling_style(db, "YBK Jaiswal", style="chinaman")
    assert "left-arm spin" in STYLE_GROUPS


def test_player_by_year_consistency_per_format(db):
    from cricassist.stats.queries import player_by_year

    split = player_by_year(db, "YBK Jaiswal", "batting", by_format=True, min_matches=1)
    assert "consistency" not in split  # years are only compared within a format
    assert set(split["totals_by_format"]) == {"TEST", "T20"}
    assert all("consistency" in t for t in split["totals_by_format"].values())
    whole = player_by_year(db, "YBK Jaiswal", "batting", min_matches=1)
    assert "consistency" in whole and "totals_by_format" not in whole


def test_record_lists_report_coverage(db):
    from cricassist.stats.queries import Filters, best_innings, leaders

    assert best_innings(db, "team_lowest", Filters(format="TEST"))["coverage"]["matches_in_data"] == 1
    assert leaders(db, "runs")["coverage"]["matches_in_data"] == 2  # men's matches in the test data
