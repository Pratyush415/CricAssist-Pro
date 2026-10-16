"""Command-line entry point."""

import argparse
import sys
import time
from pathlib import Path

from cricassist import __version__
from cricassist.config import load_settings


def build_db(args: argparse.Namespace) -> int:
    from cricassist.stats.ingest import IngestError, build_database

    db_path = Path(args.db_path) if args.db_path else load_settings().db_path
    start = time.monotonic()
    try:
        report = build_database(Path(args.raw_dir), db_path, checksums=not args.skip_checksums)
    except IngestError as e:
        print("Build failed:", file=sys.stderr)
        for problem in e.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    for table, rows in report.row_counts.items():
        print(f"  {table:<32} {rows:>12,}")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    print(f"Built {report.db_path} in {time.monotonic() - start:.0f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cricassist", description="CricAssist-Pro")
    parser.add_argument("--version", action="version", version=f"CricAssist-Pro {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("build-db", help="build the stats database from the ball-by-ball CSVs")
    p.add_argument("--raw-dir", default="data/raw", help="folder with the CSV files (default: data/raw)")
    p.add_argument("--db-path", help="database file to write (default: CRICASSIST_DB_PATH)")
    p.add_argument("--skip-checksums", action="store_true", help="check file sizes only, not SHA-256")
    p.set_defaults(func=build_db)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
