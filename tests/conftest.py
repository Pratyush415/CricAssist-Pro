import json
from pathlib import Path

import pytest

from cricassist.stats.db import connect
from cricassist.stats.ingest import build_database

DATA_DIR = Path(__file__).parent / "data"
TEST_MANIFEST = json.loads((DATA_DIR / "manifest.json").read_text("utf-8"))

KANPUR_TEST = 1439892  # India v Bangladesh, 2024
DC_V_RR = 1473469  # IPL 2025, tied and decided by a super over
WI_V_ENG_WOMEN = 1448396  # ODI 2025, D/L result


def build(raw_dir: Path, db_path: Path, manifest: dict = TEST_MANIFEST):
    return build_database(raw_dir, db_path, manifest=manifest, log=lambda msg: None)


@pytest.fixture(scope="session")
def db(tmp_path_factory):
    """A read-only connection to a database built from the three-match test subset."""
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    report = build(DATA_DIR, db_path)
    assert report.warnings == []
    con = connect(db_path)
    yield con
    con.close()
