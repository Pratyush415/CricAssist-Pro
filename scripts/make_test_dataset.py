"""One-off script: cut a few matches out of data/raw/ into tests/data/ for the test suite.

Usage: python scripts/make_test_dataset.py [match_id ...]

Copies every row for the chosen matches from each CSV, plus the players they
reference, and writes tests/data/manifest.json in the same format as the real
manifest so the loader's checks run on the test data too.
"""

import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
OUT_DIR = ROOT / "tests" / "data"
MANIFEST = ROOT / "src" / "cricassist" / "stats" / "dataset" / "manifest.json"

# IPL 2025 DC v RR (tie, super over, tracking, reviews, substitutes),
# Test 2024 India v Bangladesh at Kanpur (four innings, declarations),
# Women's ODI 2025 West Indies v England (D/L result, powerplays).
DEFAULT_MATCHES = ["1473469", "1439892", "1448396"]

PLAYER_FILES = ["players.csv", "player_external_ids.csv"]


def copy_rows(src: Path, dst: Path, keep) -> int:
    rows = 0
    with open(src, newline="", encoding="utf-8") as fin, open(dst, "w", newline="", encoding="utf-8") as fout:
        reader = csv.reader(fin)
        writer = csv.writer(fout, lineterminator="\n")
        header = next(reader)
        writer.writerow(header)
        for row in reader:
            if keep(header, row):
                writer.writerow(row)
                rows += 1
    return rows


def main(match_ids: list[str]) -> None:
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    names = [f["name"] for f in manifest["files"] if f["name"].endswith(".csv")]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wanted = set(match_ids)
    player_ids: set[str] = set()

    def keep_match(header, row):
        if row[0] not in wanted:
            return False
        for col, value in zip(header, row):
            if value and col != "match_id" and (col.endswith("_id") or col == "player_id"):
                player_ids.add(value)
        return True

    rows = {}
    for name in names:
        if name in PLAYER_FILES:
            continue
        print(f"{name}...")
        keep = keep_match
        if name == "coverage_report.csv":  # match_id is the third column here
            keep = lambda header, row: row[2] in wanted  # noqa: E731
        rows[name] = copy_rows(RAW_DIR / name, OUT_DIR / name, keep)

    for name in PLAYER_FILES:
        rows[name] = copy_rows(RAW_DIR / name, OUT_DIR / name, lambda header, row: row[0] in player_ids)

    files = []
    for name in names:
        data = (OUT_DIR / name).read_bytes()
        files.append({"name": name, "rows": rows[name], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    out = {
        "dataset": manifest["dataset"] + " (test subset)",
        "snapshot_date": manifest["snapshot_date"],
        "matches": sorted(int(m) for m in wanted),
        "files": files,
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(out, indent=1) + "\n", "utf-8", newline="\n")
    for f in files:
        print(f"  {f['name']:<32} {f['rows']:>6}")


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT_MATCHES)
