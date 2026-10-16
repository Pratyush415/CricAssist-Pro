import json
import shutil

import pytest

from conftest import DATA_DIR, DC_V_RR, KANPUR_TEST, TEST_MANIFEST, build
from cricassist.main import main
from cricassist.stats.db import column_name, load_manifest, load_schema
from cricassist.stats.ingest import IngestError, verify_files, verify_headers

CSV_NAMES = [f["name"] for f in TEST_MANIFEST["files"]]


@pytest.fixture
def raw_copy(tmp_path):
    raw = tmp_path / "raw"
    shutil.copytree(DATA_DIR, raw)
    return raw


def test_packaged_schema_and_manifest_agree():
    csvs = [f["name"] for f in load_manifest()["files"] if f["name"].endswith(".csv")]
    assert sorted(csvs) == sorted(load_schema()["files"])
    assert sorted(CSV_NAMES) == sorted(csvs)


def test_row_counts_match_manifest(db):
    for entry in TEST_MANIFEST["files"]:
        table = entry["name"].removesuffix(".csv")
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == entry["rows"], table


def test_columns_are_typed_and_sql_friendly(db):
    cols = {name: dtype for name, dtype, *_ in db.execute("DESCRIBE ball_by_ball_data").fetchall()}
    assert cols["is_legal_delivery"] == "BOOLEAN"
    assert cols["start_date"] == "DATE"
    assert cols["scorecard_ball"] == "DOUBLE"
    assert cols["striker_balls_faced"] == "BIGINT"  # "striker balls faced" in the CSV
    assert cols["batting_style_striker"] == "VARCHAR"
    tracking = {name: dtype for name, dtype, *_ in db.execute("DESCRIBE cricinfo_tracking").fetchall()}
    assert tracking["ball_timestamp"] == "TIMESTAMP"


def test_player_ids_keep_leading_zeros(db):
    ids = [r[0] for r in db.execute("SELECT player_id FROM players").fetchall()]
    assert all(isinstance(i, str) and len(i) == 8 for i in ids)


def test_kanpur_test_innings_totals(db):
    rows = db.execute(
        """SELECT innings, batting_team, sum(runs_off_bat + extras), sum(wicket)
           FROM ball_by_ball_data WHERE match_id = ? GROUP BY ALL ORDER BY innings""",
        [KANPUR_TEST],
    ).fetchall()
    assert rows == [
        (1, "Bangladesh", 233, 10),
        (2, "India", 285, 9),
        (3, "Bangladesh", 146, 10),
        (4, "India", 98, 3),
    ]
    declared = db.execute(
        "SELECT innings FROM innings WHERE match_id = ? AND declared", [KANPUR_TEST]
    ).fetchall()
    assert declared == [(2,)]


def test_super_over_innings(db):
    rows = db.execute(
        """SELECT i.innings, i.is_super_over, sum(b.runs_off_bat + b.extras)
           FROM innings i JOIN ball_by_ball_data b USING (match_id, innings)
           WHERE match_id = ? GROUP BY ALL ORDER BY i.innings""",
        [DC_V_RR],
    ).fetchall()
    assert rows == [(1, False, 188), (2, False, 188), (3, True, 11), (4, True, 13)]


def test_dataset_info(db):
    info = dict(db.execute("SELECT key, value FROM dataset_info").fetchall())
    assert info["snapshot_date"] == TEST_MANIFEST["snapshot_date"]
    assert "Cricsheet" in info["source"]


def test_column_name():
    assert column_name("striker balls faced") == "striker_balls_faced"
    assert column_name("batting style_striker") == "batting_style_striker"
    assert column_name("match_id") == "match_id"


def test_verify_files_passes_on_test_data():
    assert verify_files(DATA_DIR, TEST_MANIFEST) == []
    assert verify_headers(DATA_DIR, load_schema(), CSV_NAMES) == []


def test_verify_files_catches_missing_resized_and_altered_files(raw_copy):
    (raw_copy / "reviews.csv").unlink()
    with open(raw_copy / "innings.csv", "ab") as f:
        f.write(b"\n")
    data = bytearray((raw_copy / "matches.csv").read_bytes())
    data[-2] = ord("X") if data[-2] != ord("X") else ord("Y")  # same size, different bytes
    (raw_copy / "matches.csv").write_bytes(bytes(data))

    problems = verify_files(raw_copy, TEST_MANIFEST)
    assert any(p.startswith("reviews.csv: missing") for p in problems)
    assert any(p.startswith("innings.csv:") and "bytes" in p for p in problems)
    assert any(p.startswith("matches.csv: SHA-256") for p in problems)
    assert not any(p.startswith("matches.csv") for p in verify_files(raw_copy, TEST_MANIFEST, checksums=False))


def test_verify_headers_catches_renamed_column(raw_copy):
    path = raw_copy / "innings.csv"
    path.write_text(path.read_text("utf-8").replace("declared", "declaration", 1), "utf-8", newline="")
    problems = verify_headers(raw_copy, load_schema(), ["innings.csv"])
    assert problems and "declared" in problems[0] and "declaration" in problems[0]


def test_failed_build_keeps_existing_database(raw_copy, tmp_path):
    db_path = tmp_path / "out.db"
    build(raw_copy, db_path)
    before = db_path.read_bytes()

    manifest = json.loads(json.dumps(TEST_MANIFEST))
    next(f for f in manifest["files"] if f["name"] == "matches.csv")["rows"] += 1
    with pytest.raises(IngestError, match="matches: loaded 3 rows, manifest says 4"):
        build(raw_copy, db_path, manifest)
    assert db_path.read_bytes() == before
    assert not (tmp_path / "out.db.building").exists()


def test_cli_build_db_reports_failure(tmp_path, capsys):
    # The packaged manifest describes the full dataset, so the test subset must fail its checks.
    code = main(["build-db", "--raw-dir", str(DATA_DIR), "--db-path", str(tmp_path / "x.db")])
    assert code == 1
    assert "bytes, manifest says" in capsys.readouterr().err
    assert not (tmp_path / "x.db").exists()
