"""Database connection and schema setup."""

import json
import re
from functools import cache
from importlib import resources
from pathlib import Path

import duckdb

# schema.json column types -> DuckDB types. Timestamps in the dataset are UTC.
DUCKDB_TYPES = {
    "integer": "BIGINT",
    "number": "DOUBLE",
    "string": "VARCHAR",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
}


def _dataset_json(name: str) -> dict:
    path = resources.files("cricassist").joinpath("stats", "dataset", name)
    return json.loads(path.read_text("utf-8"))


@cache
def load_schema() -> dict:
    """The dataset schema shipped with the package (stats/dataset/schema.json)."""
    return _dataset_json("schema.json")


@cache
def load_manifest() -> dict:
    """The dataset manifest shipped with the package (stats/dataset/manifest.json)."""
    return _dataset_json("manifest.json")


def table_name(csv_name: str) -> str:
    """`ball_by_ball_data.csv` -> `ball_by_ball_data`."""
    return Path(csv_name).stem


def column_name(csv_column: str) -> str:
    """Make a CSV header usable in SQL: `batting style_striker` -> `batting_style_striker`."""
    return re.sub(r"[^0-9a-z]+", "_", csv_column.lower()).strip("_")


def create_table_sql(csv_name: str, spec: dict) -> str:
    """CREATE TABLE statement for one CSV described in schema.json."""
    cols = []
    for col in spec["columns"]:
        null = "" if col["nullable"] else " NOT NULL"
        cols.append(f'"{column_name(col["name"])}" {DUCKDB_TYPES[col["type"]]}{null}')
    return f'CREATE TABLE "{table_name(csv_name)}" (\n  ' + ",\n  ".join(cols) + "\n)"


def connect(db_path: Path, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """Open the stats database. Read-only unless building it."""
    if read_only and not Path(db_path).exists():
        raise FileNotFoundError(f"No database at {db_path}. Run `cricassist build-db` first.")
    return duckdb.connect(str(db_path), read_only=read_only)
