"""SQLPad command-line entry point.

Usage:
  python -m sqlpad watch   [query.sql]      live .sql file runner (default)
  python -m sqlpad repl                     interactive SQL prompt
  python -m sqlpad import  <csv> <table>    load a CSV into a table
  python -m sqlpad run     "<sql>"          run one query and print it
  python -m sqlpad tables                   list tables

Pick a dialect to practice with -d/--dialect: standard (DuckDB), mysql, or
bigquery. For watch/repl, if you omit it you'll get a small menu.

The database persists in practice.duckdb (override with --db or $SQLPAD_DB).
"""

from __future__ import annotations

import argparse
import sys

from . import core, dialects, render, repl as repl_mod, watch as watch_mod


def _choose_dialect() -> dialects.Dialect:
    """Interactive entry menu (only used when stdin is a real terminal)."""
    ordered = [dialects.DIALECTS[k] for k in ("standard", "mysql", "bigquery")]
    print("Which SQL dialect do you want to practice?")
    for i, d in enumerate(ordered, start=1):
        print(f"  {i}) {d.title}")
    try:
        choice = input("> ").strip()
    except EOFError:
        choice = ""
    mapping = {"1": "standard", "2": "mysql", "3": "bigquery"}
    key = mapping.get(choice, choice or "standard")
    try:
        return dialects.resolve(key)
    except ValueError:
        print("(not recognised — using Standard / DuckDB)")
        return dialects.DIALECTS["standard"]


def _resolve_dialect(arg: str | None, interactive_ok: bool) -> dialects.Dialect:
    if arg:
        return dialects.resolve(arg)
    if interactive_ok and sys.stdin.isatty():
        return _choose_dialect()
    return dialects.DIALECTS[dialects.DEFAULT_DIALECT]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlpad", description="Local SQL practice with DuckDB.")
    parser.add_argument("--db", default=core.DEFAULT_DB, help="database file (default: practice.duckdb)")
    parser.add_argument("-d", "--dialect", default=None,
                        help="standard | mysql | bigquery (aliases: duckdb, bq)")
    sub = parser.add_subparsers(dest="cmd")

    p_watch = sub.add_parser("watch", help="watch a .sql file and re-run on save")
    p_watch.add_argument("file", nargs="?", default=None,
                         help="defaults to the dialect's scratch file")

    sub.add_parser("repl", help="interactive SQL prompt")

    p_import = sub.add_parser("import", help="load a CSV into a table")
    p_import.add_argument("csv")
    p_import.add_argument("table")

    p_run = sub.add_parser("run", help="run one query and print results")
    p_run.add_argument("sql")

    sub.add_parser("tables", help="list tables")

    args = parser.parse_args(argv)
    cmd = args.cmd or "watch"   # default action is the watcher

    try:
        if cmd == "watch":
            dial = _resolve_dialect(args.dialect, interactive_ok=True)
            watch_mod.watch(getattr(args, "file", None), db_path=args.db, dialect=dial)
            return 0

        if cmd == "repl":
            dial = _resolve_dialect(args.dialect, interactive_ok=True)
            repl_mod.repl(db_path=args.db, dialect=dial)
            return 0

        # non-interactive commands: dialect only matters for its prelude macros
        dial = _resolve_dialect(args.dialect, interactive_ok=False)

        if cmd == "import":
            con = core.connect(args.db, prelude=dial.prelude)
            try:
                n = core.import_csv(con, args.csv, args.table)
                print(f"loaded {n} rows into '{args.table}' (db: {args.db})")
            finally:
                con.close()
            return 0

        if cmd == "run":
            con = core.connect(args.db, prelude=dial.prelude)
            try:
                for stmt in render.split_statements(args.sql):
                    res = core.run_statement(con, stmt)
                    if not res.ok:
                        print(f"Error: {res.error}", file=sys.stderr)
                        return 1
                    if res.has_rows:
                        print(render.render_table(res.columns, res.rows))
                    else:
                        print("ok")
            finally:
                con.close()
            return 0

        if cmd == "tables":
            con = core.connect(args.db)
            try:
                tables = core.list_tables(con)
                print("\n".join(tables) if tables else "(no tables)")
            finally:
                con.close()
            return 0
    except ValueError as exc:   # unknown dialect
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
