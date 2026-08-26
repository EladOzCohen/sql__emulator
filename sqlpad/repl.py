"""Interactive SQL REPL with history, syntax highlighting and meta-commands.

Feels like the mysql client: type SQL across multiple lines, press Enter to
run once the statement ends with ';'. Lines starting with '.' are meta-commands.
"""

from __future__ import annotations

import os

from . import core, render

# prompt_toolkit / pygments are part of Anaconda; degrade gracefully if absent.
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.lexers import PygmentsLexer
    from pygments.lexers.sql import SqlLexer
    _HAVE_PTK = True
except Exception:  # noqa: BLE001
    _HAVE_PTK = False


HELP = """
Meta-commands:
  .help                 show this help
  .tables               list tables
  .schema [table]       show columns for a table (or all tables)
  .import <csv> <table> load a CSV file into a table
  .read <file.sql>      run all statements from a .sql file
  .width <n>            set max column width (default 40)
  .quit / .exit         leave

Everything else is SQL. End a statement with ';' to run it.
Multi-line is fine — press Enter mid-statement to keep typing.
"""


def _print_result(res: core.Result, max_width: int) -> None:
    if not res.ok:
        print(f"\033[31m✗ {res.error}\033[0m")
        return
    if res.has_rows:
        print(render.render_table(res.columns, res.rows, max_col_width=max_width))
        print(f"\033[90m{res.elapsed_ms:.1f} ms\033[0m")
    else:
        print(f"\033[32m✓ ok\033[0m  \033[90m{res.elapsed_ms:.1f} ms\033[0m")


def _run_sql(con, sql: str, max_width: int) -> None:
    for stmt in render.split_statements(sql):
        res = core.run_statement(con, stmt)
        _print_result(res, max_width)
        if not res.ok:
            break


def _handle_meta(con, line: str, state: dict) -> bool:
    """Return True if the app should keep running, False to quit."""
    parts = line.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in (".quit", ".exit", ".q"):
        return False
    if cmd == ".help":
        print(HELP)
    elif cmd == ".tables":
        tables = core.list_tables(con)
        if tables:
            print(render.render_table(["table"], [(t,) for t in tables]))
        else:
            print("(no tables yet — .import a CSV or CREATE one)")
    elif cmd == ".schema":
        targets = args if args else core.list_tables(con)
        if not targets:
            print("(no tables yet)")
        for t in targets:
            print(f"\n{t}:")
            res = core.table_schema(con, t)
            _print_result(res, state["width"])
    elif cmd == ".import":
        if len(args) != 2:
            print("usage: .import <csv_path> <table_name>")
        else:
            try:
                n = core.import_csv(con, args[0], args[1])
                print(f"\033[32m✓ loaded {n} rows into '{args[1]}'\033[0m")
            except Exception as exc:  # noqa: BLE001
                print(f"\033[31m✗ {exc}\033[0m")
    elif cmd == ".read":
        if not args:
            print("usage: .read <file.sql>")
        elif not os.path.exists(args[0]):
            print(f"\033[31m✗ no such file: {args[0]}\033[0m")
        else:
            with open(args[0], "r", encoding="utf-8") as fh:
                _run_sql(con, fh.read(), state["width"])
    elif cmd == ".width":
        if args and args[0].isdigit():
            state["width"] = int(args[0])
            print(f"max column width = {state['width']}")
        else:
            print("usage: .width <n>")
    else:
        print(f"unknown command: {cmd} (try .help)")
    return True


def _make_session(history_path: str):
    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        """Submit on ';' or on a meta-command; otherwise insert a newline."""
        buf = event.current_buffer
        text = buf.text.strip()
        if text.endswith(";") or text.startswith("."):
            buf.validate_and_handle()
        else:
            buf.insert_text("\n")

    return PromptSession(
        history=FileHistory(history_path),
        auto_suggest=AutoSuggestFromHistory(),
        lexer=PygmentsLexer(SqlLexer),
        key_bindings=kb,
        multiline=True,
        include_default_pygments_style=False,
    )


def repl(db_path: str = core.DEFAULT_DB) -> None:
    con = core.connect(db_path)
    state = {"width": 40}
    tables = core.list_tables(con)

    print("SQLPad REPL — DuckDB.  Type .help for commands, .quit to leave.")
    print(f"db: {os.path.abspath(db_path)}")
    if tables:
        print("tables: " + ", ".join(tables))
    print()

    if _HAVE_PTK:
        session = _make_session(
            os.path.join(os.path.dirname(os.path.abspath(db_path)),
                         ".sqlpad_history"))
    else:
        session = None
        print("(prompt_toolkit unavailable — using basic input, "
              "end statements with ';')")

    try:
        while True:
            try:
                if session is not None:
                    line = session.prompt("sql> ")
                else:
                    line = input("sql> ")
            except EOFError:
                break

            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("."):
                if not _handle_meta(con, stripped, state):
                    break
                continue

            _run_sql(con, line, state["width"])
    except KeyboardInterrupt:
        pass
    finally:
        con.close()
        print("\nbye.")
