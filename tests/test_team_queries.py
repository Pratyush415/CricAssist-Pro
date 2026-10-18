import pytest

from conftest import DC_V_RR, KANPUR_TEST
from cricassist.stats.queries import (
    Filters,
    TeamNotFound,
    batting_summary,
    best_innings,
    delivery_breakdown,
    leaders,
    player_phase_stats,
    resolve_team,
    team_head_to_head,
    team_phase_stats,
    team_results,
    venue_summary,
)

# --- Team names --------------------------------------------------------------


@pytest.mark.parametrize(
    "name, team",
    [
        ("Delhi Daredevils", "Delhi Capitals"),
        ("delhi capitals", "Delhi Capitals"),
        ("DC", "Delhi Capitals"),
        ("RR", "Rajasthan Royals"),
        ("Kings XI Punjab", "Punjab Kings"),
        ("Royal Challengers Bangalore", "Royal Challengers Bengaluru"),
        ("Bangla", "Bangladesh"),  # part of a name, when only one team matches
    ],
)
def test_resolve_team(db, name, team):
    assert resolve_team(db, name) == team


def test_resolve_team_errors(db):
    with pytest.raises(TeamNotFound) as missing:
        resolve_team(db, "Afghanistan")
    assert "no Afghanistan international matches" in str(missing.value)
    with pytest.raises(TeamNotFound) as ambiguous:
        resolve_team(db, "Royals")  # Rajasthan Royals, Barbados Royals
    assert len(ambiguous.value.candidates) > 1


def test_opposition_filter_accepts_old_and_short_names(db):
    for name in ("Delhi Capitals", "Delhi Daredevils", "DC"):
        assert batting_summary(db, "YBK Jaiswal", Filters(opposition=name))["matches"] == 1


# --- Results -----------------------------------------------------------------


def test_head_to_head_tie_decided_by_super_over(db):
    h2h = team_head_to_head(db, "Delhi Daredevils", "RR")
    assert h2h["team"] == "Delhi Capitals"
    r = h2h["results_for_team"]
    assert (r["played"], r["won"], r["lost"], r["tied"], r["ties_won_in_super_over"]) == (1, 0, 0, 1, 1)
    meeting = h2h["recent_meetings"][0]
    assert meeting["result"] == "tied (Delhi Capitals won the super over)"
    # Rajasthan's Samson retired hurt, which is not a wicket.
    assert meeting["scores"] == "Delhi Capitals 188/5 (20 ov); Rajasthan Royals 188/4 (20 ov)"


def test_head_to_head_test_match(db):
    h2h = team_head_to_head(db, "Bangladesh", "India", Filters(format="TEST"))
    assert (h2h["results_for_team"]["played"], h2h["results_for_team"]["lost"]) == (1, 1)
    meeting = h2h["recent_meetings"][0]
    assert meeting["result"] == "India won by 7 wickets"
    assert meeting["scores"] == "Bangladesh 233 & 146; India 285/9d & 98/3"


def test_team_queries_default_to_mens_cricket(db):
    assert team_head_to_head(db, "England", "West Indies")["results_for_team"]["played"] == 0
    women = team_head_to_head(db, "England", "West Indies", Filters(gender="women"))
    assert women["results_for_team"]["won"] == 1
    assert women["recent_meetings"][0]["result"] == "England won by 9 wickets (D/L)"


def test_team_results(db):
    r = team_results(db, "India")
    assert (r["overall"]["played"], r["overall"]["won"]) == (1, 1)
    assert [o["opposition"] for o in r["by_opposition"]] == ["Bangladesh"]


# --- Venues and records ------------------------------------------------------


def test_venue_summary(db):
    v = venue_summary(db, "green park")
    assert v["grounds_matched"] == ["Green Park, Kanpur"]
    assert (v["results"]["matches"], v["results"]["won_batting_second"]) == (1, 1)
    assert [i["average_score"] for i in v["by_innings"]] == [233.0, 285.0, 146.0, 98.0]
    assert v["highest_totals"][0]["score"] == "285/9d"
    assert [c["score"] for c in v["highest_successful_chases"]] == ["98/3"]
    assert v["top_run_scorers"][0]["player"] == "YBK Jaiswal"


def test_venue_summary_unknown(db):
    with pytest.raises(LookupError):
        venue_summary(db, "Lord's")


def test_best_innings(db):
    assert best_innings(db, "batting", limit=1)["records"][0]["score"] == "107* (194)"
    assert best_innings(db, "bowling", Filters(format="TEST"), limit=1)["records"][0]["figures"] == "4/41 (6.4 ov)"
    lowest = best_innings(db, "team_lowest", Filters(format="TEST"), limit=4)["records"]
    # 285/9d was declared and 98/3 was the winning chase, so neither is a completed innings.
    assert [r["score"] for r in lowest] == ["146", "233"]
    with pytest.raises(ValueError):
        best_innings(db, "fielding")


def test_leaders(db):
    top = leaders(db, "runs", limit=1)["leaders"][0]
    assert (top["player"], top["runs"]) == ("YBK Jaiswal", 174)
    assert leaders(db, "wickets", Filters(format="TEST"), limit=1)["leaders"][0]["wickets"] == 6  # Bumrah
    with pytest.raises(ValueError):
        leaders(db, "catches")


def test_leaders_rate_stats_need_minimum_balls(db):
    result = leaders(db, "strike_rate", Filters(competition="ipl"))
    assert result["minimum_balls"] == 120
    assert result["leaders"] == []  # nobody faced 120 balls in one match
    assert leaders(db, "strike_rate", Filters(competition="ipl"), min_balls=20)["leaders"]


# --- Phases ------------------------------------------------------------------


def test_phases_split_an_innings(db):
    by_phase = team_phase_stats(db, "Delhi Capitals", "batting", Filters(format="T20"))["by_phase"]
    assert [p["phase"] for p in by_phase] == ["powerplay", "middle", "death"]
    off_bat = db.execute(
        "SELECT sum(runs_off_bat) FROM deliveries WHERE match_id = ? AND innings = 1", [DC_V_RR]
    ).fetchone()[0]
    assert sum(p["runs"] for p in by_phase) == off_bat


def test_player_phase_stats(db):
    starc = player_phase_stats(db, "Starc", "bowling")["by_phase"]
    assert sum(p["balls"] for p in starc) == 24  # four overs
    assert sum(p["wickets"] for p in starc) == db.execute(
        "SELECT wickets FROM bowling_innings WHERE match_id = ? AND player = 'MA Starc'", [DC_V_RR]
    ).fetchone()[0]


def test_tests_have_no_phases(db):
    assert player_phase_stats(db, "JJ Bumrah", "bowling", Filters(format="TEST"))["by_phase"] == []


def test_leaders_by_phase(db):
    death = leaders(db, "wickets", Filters(competition="ipl"), phase="death", limit=3)["leaders"]
    assert death and all(r["wickets"] >= 1 for r in death)
    with pytest.raises(ValueError):
        leaders(db, "runs", phase="end overs")


# --- Line, length and shots --------------------------------------------------


def test_delivery_breakdown_by_length(db):
    result = delivery_breakdown(db, "YBK Jaiswal", "length")
    lengths = [r["length"] for r in result["breakdown"]]
    order = ["yorker", "full toss", "full", "good length", "short of a good length", "short"]
    assert lengths == [length for length in order if length in lengths]
    assert sum(r["balls"] for r in result["breakdown"]) == result["coverage"]["balls_with_this_data"]
    assert result["coverage"]["balls_with_this_data"] <= result["coverage"]["balls_in_total"]


def test_delivery_breakdown_by_shot_for_a_bowler(db):
    result = delivery_breakdown(db, "JJ Bumrah", "shot", role="bowling", filters=Filters(format="TEST"))
    assert sum(r["dismissals"] for r in result["breakdown"]) <= 6
    assert all("economy" in r for r in result["breakdown"])
    with pytest.raises(ValueError):
        delivery_breakdown(db, "JJ Bumrah", "speed")


def test_kanpur_is_one_match_everywhere(db):
    assert team_results(db, "Bangladesh", Filters(format="TEST"))["overall"]["played"] == 1
    assert venue_summary(db, "Kanpur")["coverage"]["matches_in_data"] == 1
    assert db.execute("SELECT DISTINCT match_id FROM team_innings WHERE venue LIKE '%Kanpur%'").fetchall() == [(KANPUR_TEST,)]
