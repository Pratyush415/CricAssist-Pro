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


def build_context(args: argparse.Namespace) -> int:
    from cricassist.context.build import build_context as run_build
    from cricassist.context.build import load_scrape_list
    from cricassist.context.store import ContextStore
    from cricassist.sources.cricinfo_client import CricinfoClient

    settings = load_settings()
    store = ContextStore(settings.context_path)
    if args.reset:
        store.clear()
    client = None if args.local_only else CricinfoClient(settings.cache_dir, delay=args.delay)
    report = run_build(
        store,
        client=client,
        scrape_list=None if args.local_only else load_scrape_list(),
        source_dir=Path(args.source_dir) if args.source_dir else None,
        fetch=not args.no_fetch,
        limit=args.limit,
    )
    if client is not None:
        print(f"  pages on the list:   {report.pages_listed}")
        print(f"  fetched now:         {report.fetched}")
        print(f"  from the cache:      {report.from_cache}")
        print(f"  not found:           {report.not_found}")
        print(f"  no article text:     {report.no_article_text}")
        if report.refused_reason:
            print(f"  not collected:       {report.pages_listed - report.fetched - report.from_cache - report.not_found}")
            print(f"  reason: {report.refused_reason}")
    if args.source_dir:
        print(f"  local documents:     {report.local_documents}")
    print(f"Added {report.passages} passages from {report.documents} documents; "
          f"{report.total_passages} passages in {settings.context_path}")
    return 0


def evaluate(args: argparse.Namespace) -> int:
    import json

    from cricassist.assistant.agent import Assistant, AssistantError, make_backend
    from cricassist.evaluation import load_questions, run_evaluation, summarise
    from cricassist.stats.db import connect

    settings = load_settings()
    questions = load_questions()
    if args.only:
        wanted = {w.strip() for w in args.only.split(",")}
        questions = [q for q in questions if q["id"] in wanted]
    try:
        con = connect(settings.db_path)
        model = make_backend(settings, args.provider, args.model).model  # checks the key early
    except (FileNotFoundError, AssistantError) as e:
        print(e, file=sys.stderr)
        return 1

    results_path = Path(args.results) if args.results else Path("data/processed/eval") / f"results-{model}.json"
    results: dict[str, dict] = {}
    if results_path.is_file() and not args.fresh:
        results = json.loads(results_path.read_text("utf-8"))
    if args.summary:
        print(summarise(results, questions))
        return 0
    if args.redo:
        for qid in (w.strip() for w in args.redo.split(",")):
            results.pop(qid, None)  # forget the old answer so it runs again
        questions = [q for q in questions if q["id"] in {w.strip() for w in args.redo.split(",")}]
    results_path.parent.mkdir(parents=True, exist_ok=True)

    def save(grade):
        results[grade.id] = {**grade.to_dict(), "model": model}
        results_path.write_text(json.dumps(results, indent=1, ensure_ascii=False), "utf-8")
        status = grade.error or f"correct={'yes' if grade.correct else 'NO'} sourced={'yes' if grade.sourced else 'NO'}"
        print(f"    {status}", flush=True)

    def make_assistant():
        backend = make_backend(settings, args.provider, args.model)
        if hasattr(backend, "on_wait"):
            backend.on_wait = lambda message: print(f"    ({message})", flush=True)
        return Assistant(con, backend)

    run_evaluation(make_assistant, questions, previous=results, pause=args.pause, on_result=save)
    print()
    print(summarise(results, questions))
    print(f"\nFull answers: {results_path}")
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

    p = sub.add_parser("build-context", help="build the vector database of match reports and player profiles")
    p.add_argument("--no-fetch", action="store_true", help="use cached pages only; make no web requests")
    p.add_argument("--source-dir", help="also add documents you may use from this folder (.html, .txt, .md, .jsonl)")
    p.add_argument("--local-only", action="store_true", help="skip the ESPNcricinfo scrape list; use --source-dir only")
    p.add_argument("--limit", type=int, help="only the first N pages of the scrape list")
    p.add_argument("--delay", type=float, default=5.0, help="seconds between requests (default 5, minimum 3)")
    p.add_argument("--reset", action="store_true", help="empty the vector database first")
    p.set_defaults(func=build_context)

    p = sub.add_parser("eval", help="run the 15-question test set and grade the answers")
    p.add_argument("--provider", choices=["gemini", "claude"], help="AI provider (default: CRICASSIST_LLM_PROVIDER)")
    p.add_argument("--model", help="model name (default: CRICASSIST_LLM_MODEL or the provider's default)")
    p.add_argument("--only", help="comma-separated question ids to run")
    p.add_argument("--results", help="results file (default: data/processed/eval/results-<model>.json)")
    p.add_argument("--fresh", action="store_true", help="ignore earlier results instead of resuming")
    p.add_argument("--redo", help="comma-separated question ids to run again, replacing their earlier results")
    p.add_argument("--pause", type=float, default=10.0, help="seconds between questions (default 10, for free-tier limits)")
    p.add_argument("--summary", action="store_true", help="print the results table without running anything")
    p.set_defaults(func=evaluate)

    p = sub.add_parser("chat", help="ask the assistant questions about the stats")
    p.add_argument("question", nargs="*", help="ask one question and exit; leave out for a conversation")
    p.add_argument("--provider", choices=["gemini", "claude"], help="AI provider (default: CRICASSIST_LLM_PROVIDER)")
    p.add_argument("--model", help="model name (default: CRICASSIST_LLM_MODEL or the provider's default)")
    p.add_argument("--hide-tools", action="store_true", help="do not print the stats tools the assistant calls")
    p.set_defaults(func=chat)

    args = parser.parse_args(argv)
    if getattr(args, "delay", None) is not None and args.delay < 3:
        parser.error("--delay must be at least 3 seconds")
    if not args.command:
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
