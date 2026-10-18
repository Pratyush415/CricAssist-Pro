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


def _format_call(call) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in call.args.items())
    return f"  · {call.name}({args})" + ("  [error]" if call.is_error else "")


def chat(args: argparse.Namespace) -> int:
    from cricassist.assistant.agent import Assistant, AssistantError, make_backend
    from cricassist.stats.db import connect

    settings = load_settings()
    try:
        con = connect(settings.db_path)
        backend = make_backend(settings, args.provider, args.model)
    except (FileNotFoundError, AssistantError) as e:
        print(e, file=sys.stderr)
        return 1

    def show_call(call):
        if not args.hide_tools:
            print(_format_call(call), flush=True)

    assistant = Assistant(con, backend, on_tool_call=show_call)
    if hasattr(backend, "on_wait"):
        backend.on_wait = lambda message: print(f"  ({message})", flush=True)

    def answer(question: str) -> bool:
        try:
            print(f"\n{assistant.ask(question).text.strip()}\n", flush=True)
            return True
        except AssistantError as e:
            print(f"\n{e}\n", file=sys.stderr)
            return False

    if args.question:
        return 0 if answer(" ".join(args.question)) else 1

    print(f"CricAssist-Pro chat ({backend.model}). Ask a question, or type 'exit' to quit.\n")
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question.lower() in ("exit", "quit"):
            return 0
        if question:
            answer(question)


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; player names need UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="cricassist", description="CricAssist-Pro")
    parser.add_argument("--version", action="version", version=f"CricAssist-Pro {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("build-db", help="build the stats database from the ball-by-ball CSVs")
    p.add_argument("--raw-dir", default="data/raw", help="folder with the CSV files (default: data/raw)")
    p.add_argument("--db-path", help="database file to write (default: CRICASSIST_DB_PATH)")
    p.add_argument("--skip-checksums", action="store_true", help="check file sizes only, not SHA-256")
    p.set_defaults(func=build_db)

    p = sub.add_parser("chat", help="ask the assistant questions about the stats")
    p.add_argument("question", nargs="*", help="ask one question and exit; leave out for a conversation")
    p.add_argument("--provider", choices=["gemini", "claude"], help="AI provider (default: CRICASSIST_LLM_PROVIDER)")
    p.add_argument("--model", help="model name (default: CRICASSIST_LLM_MODEL or the provider's default)")
    p.add_argument("--hide-tools", action="store_true", help="do not print the stats tools the assistant calls")
    p.set_defaults(func=chat)

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
