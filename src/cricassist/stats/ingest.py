"""Load ball-by-ball data files into the database."""

import csv
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cricassist.stats.db import (
    DUCKDB_TYPES,
    column_name,
    connect,
    create_table_sql,
    load_manifest,
    load_schema,
    table_name,
)


class IngestError(Exception):
    """The raw files or the built database failed a check."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


@dataclass
class BuildReport:
    db_path: Path
    row_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_entries(manifest: dict) -> list[dict]:
    """Manifest entries for the CSV files (the manifest also lists schema.json)."""
    return [f for f in manifest["files"] if f["name"].endswith(".csv")]


def verify_files(raw_dir: Path, manifest: dict, checksums: bool = True) -> list[str]:
    """Check each CSV in the manifest exists in raw_dir with the right size and SHA-256."""
    problems = []
    for entry in csv_entries(manifest):
        path = raw_dir / entry["name"]
        if not path.is_file():
            problems.append(f"{entry['name']}: missing from {raw_dir}")
            continue
        size = path.stat().st_size
        if size != entry["bytes"]:
            problems.append(f"{entry['name']}: {size} bytes, manifest says {entry['bytes']}")
        elif checksums and sha256_file(path) != entry["sha256"]:
            problems.append(f"{entry['name']}: SHA-256 does not match the manifest")
    return problems


def verify_headers(raw_dir: Path, schema: dict, names: list[str]) -> list[str]:
    """Check each CSV's header row lists exactly the schema's columns, in order."""
    problems = []
    for name in names:
        with open(raw_dir / name, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        expected = [c["name"] for c in schema["files"][name]["columns"]]
        if header != expected:
            missing = [c for c in expected if c not in header]
            extra = [c for c in header if c not in expected]
            detail = f"missing {missing}, unexpected {extra}" if missing or extra else "columns out of order"
            problems.append(f"{name}: header does not match schema.json ({detail})")
    return problems


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _read_csv_sql(path: Path, spec: dict) -> str:
    columns = ", ".join(
        f"{_sql_str(c['name'])}: {_sql_str(DUCKDB_TYPES[c['type']])}" for c in spec["columns"]
    )
    return (
        f"SELECT * FROM read_csv({_sql_str(path.as_posix())}, header = true, "
        f"auto_detect = false, delim = ',', quote = '\"', escape = '\"', columns = {{{columns}}})"
    )


def _check_table(con, csv_name: str, spec: dict, expected_rows: int) -> tuple[int, list[str], list[str]]:
    """Row count and primary key checks (errors) plus foreign key checks (warnings)."""
    table = table_name(csv_name)
    errors, warnings = [], []
    rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    if rows != expected_rows:
        errors.append(f"{table}: loaded {rows} rows, manifest says {expected_rows}")

    pk = ", ".join(f'"{column_name(c)}"' for c in spec["primary_key"])
    dupes = con.execute(
        f'SELECT count(*) FROM (SELECT 1 FROM "{table}" GROUP BY {pk} HAVING count(*) > 1)'
    ).fetchone()[0]
    if dupes:
        errors.append(f"{table}: {dupes} duplicate primary keys ({pk})")

    for fk in spec.get("foreign_keys", []):
        ref_table = table_name(fk["references"]["file"])
        cols = [column_name(c) for c in fk["columns"]]
        ref_cols = [column_name(c) for c in fk["references"]["columns"]]
        on = " AND ".join(f'r."{rc}" = t."{c}"' for c, rc in zip(cols, ref_cols))
        not_null = " AND ".join(f't."{c}" IS NOT NULL' for c in cols)
        orphans = con.execute(
            f'SELECT count(*) FROM "{table}" t WHERE {not_null} '
            f'AND NOT EXISTS (SELECT 1 FROM "{ref_table}" r WHERE {on})'
        ).fetchone()[0]
        if orphans:
            warnings.append(
                f"{table}: {orphans} rows where ({', '.join(cols)}) has no match in {ref_table}"
            )
    return rows, errors, warnings


def build_database(
    raw_dir: Path,
    db_path: Path,
    *,
    schema: dict | None = None,
    manifest: dict | None = None,
    checksums: bool = True,
    log: Callable[[str], None] = print,
) -> BuildReport:
    """Verify the raw CSVs and load them into a fresh DuckDB database at db_path.

    The database is built in a temporary file and only replaces db_path once
    every check passes, so a failed build leaves any existing database alone.
    """
    schema = schema or load_schema()
    manifest = manifest or load_manifest()
    raw_dir, db_path = Path(raw_dir), Path(db_path)
    entries = csv_entries(manifest)
    names = [e["name"] for e in entries]

    unknown = [n for n in names if n not in schema["files"]]
    if unknown:
        raise IngestError([f"{n}: in the manifest but not in schema.json" for n in unknown])

    log("Checking files against the manifest" + (" (with checksums)" if checksums else "") + "...")
    problems = verify_files(raw_dir, manifest, checksums)
    if not problems:
        problems = verify_headers(raw_dir, schema, names)
    if problems:
        raise IngestError(problems)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".building")
    tmp_path.unlink(missing_ok=True)

    report = BuildReport(db_path=db_path)
    errors = []
    con = connect(tmp_path, read_only=False)
    try:
        for name in names:
            log(f"Loading {name}...")
            con.execute(create_table_sql(name, schema["files"][name]))
            con.execute(
                f'INSERT INTO "{table_name(name)}" {_read_csv_sql(raw_dir / name, schema["files"][name])}'
            )

        log("Checking row counts and keys...")
        for entry in entries:
            rows, errs, warns = _check_table(con, entry["name"], schema["files"][entry["name"]], entry["rows"])
            report.row_counts[table_name(entry["name"])] = rows
            errors += errs
            report.warnings += warns

        con.execute("CREATE TABLE dataset_info (key VARCHAR PRIMARY KEY, value VARCHAR)")
        con.executemany(
            "INSERT INTO dataset_info VALUES (?, ?)",
            [
                ("dataset", manifest["dataset"]),
                ("snapshot_date", manifest["snapshot_date"]),
                ("source", "Cricsheet (https://cricsheet.org/), ODC Attribution License 1.0"),
                ("built_at_utc", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
            ],
        )
    except BaseException:
        con.close()
        tmp_path.unlink(missing_ok=True)
        raise
    con.close()

    if errors:
        tmp_path.unlink(missing_ok=True)
        raise IngestError(errors)
    os.replace(tmp_path, db_path)
    return report
