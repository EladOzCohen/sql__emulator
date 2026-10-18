"""Interactive SQL REPL with history, syntax highlighting and meta-commands.

Feels like the mysql client: type SQL across multiple lines, press Enter to
run once the statement ends with ';'. Lines starting with '.' are meta-commands.
"""

from __future__ import annotations

import os

from . import catalog, core, dialects, render

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
  .databases            list databases (a database = a folder under data/)
  .use <db>             switch the active database (same as: USE <db>;)
  .sync                 load new/changed CSVs from data/ right now
  .reset <db>           rebuild a database from its CSVs (discards changes)
  .tables               list tables in the active database
  .schema [table]       show columns for a table (or all tables)
  .import <file> <table> [sheet]
                        load a CSV or Excel (.xlsx) file into a table
  .read <file.sql>      run all statements from a .sql file
  .dialect [name]       show the dialect cheat-sheet, or switch dialect
                        (name = standard | mysql | bigquery)
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


def _track_db(state: dict) -> None:
    """Pick up a database switch (e.g. a typed `USE x;`) and remember it."""
    try:
        cur = catalog.current_database(state["con"])
    except Exception:  # noqa: BLE001
        return
    if cur != state["db"]:
        state["db"] = cur
        catalog.remember_active(cur)


def print_sync(report: "catalog.SyncReport") -> None:
    print(f"\033[32m✓ {report.summary()}\033[0m")
    for err in report.errors:
        print(f"\033[31m  ✗ {err}\033[0m")


def show_databases(con) -> None:
    tree = catalog.describe_all(con)
    current = catalog.current_database(con)
    rows = []
    for info in catalog.list_databases(con):
        n_tables = len(tree.get(info.name, []))
        source = f"data/{info.folder.name}/" if info.folder else "(default — tables you create)"
        rows.append(("*" if info.name == current else "", info.name, n_tables, source))
    print(render.render_table(["", "database", "tables", "source"], rows))


def _handle_meta(line: str, state: dict) -> bool:
    """Return True if the app should keep running, False to quit."""
    con = state["con"]
    parts = line.strip().split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in (".quit", ".exit", ".q"):
        return False
    if cmd == ".help":
        print(HELP)
    elif cmd in (".databases", ".dbs"):
        show_databases(con)
    elif cmd == ".use":
        if len(args) != 1:
            print("usage: .use <database>   (see .databases)")
        else:
            try:
                catalog.use(con, args[0])
                state["db"] = args[0]
                print(f"\033[32m✓ using database '{args[0]}'\033[0m")
            except Exception as exc:  # noqa: BLE001
                print(f"\033[31m✗ {str(exc).splitlines()[0]}\033[0m")
    elif cmd == ".sync":
        print_sync(catalog.sync(con))
    elif cmd == ".reset":
        if len(args) != 1:
            print("usage: .reset <database>   (rebuilds it from its CSVs)")
        else:
            name = args[0]
            answer = input(f"This DISCARDS all changes in '{name}' and reloads its CSVs. "
                           f"Type the database name to confirm: ").strip()
            if answer != name:
                print("cancelled.")
            else:
                try:
                    print_sync(catalog.reset(con, name))
                except Exception as exc:  # noqa: BLE001
                    print(f"\033[31m✗ {str(exc).splitlines()[0]}\033[0m")
    elif cmd == ".tables":
        tables = core.list_tables(con)
        if tables:
            print(f"tables in '{state['db']}':")
            print(render.render_table(["table"], [(t,) for t in tables]))
        else:
            print(f"(no tables in '{state['db']}' — try .databases, or .import / CREATE one)")
    elif cmd == ".schema":
        targets = args if args else core.list_tables(con)
        if not targets:
            print("(no tables yet)")
        for t in targets:
            print(f"\n{t}:")
            res = core.table_schema(con, t)
            _print_result(res, state["width"])
    elif cmd == ".import":
        if len(args) not in (2, 3):
            print("usage: .import <csv_or_xlsx_path> <table_name> [sheet]")
        else:
            sheet: int | str = 0
            if len(args) == 3:
                sheet = int(args[2]) if args[2].isdigit() else args[2]
            try:
                n = core.import_file(con, args[0], args[1], sheet=sheet)
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
    elif cmd == ".dialect":
        if not args:
            print(f"\033[35mcurrent: {state['dialect'].title}\033[0m\n")
            print(state["dialect"].cheatsheet)
        else:
            try:
                new = dialects.resolve(args[0])
            except ValueError as exc:
                print(f"\033[31m✗ {exc}\033[0m")
                return True
            # reconnect so the previous dialect's TEMP macros are cleared;
            # persisted tables live in the db file and survive.
            con.close()
            state["con"] = core.connect(state["db_path"], prelude=new.prelude)
            state["db"] = catalog.current_database(state["con"])
            state["dialect"] = new
            print(f"\033[32m✓ switched to {new.title}\033[0m "
                  f"(.dialect to see its cheat-sheet)")
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


def repl(db_path: str = core.DEFAULT_DB,
         dialect: dialects.Dialect | None = None) -> None:
    dial = dialect or dialects.DIALECTS[dialects.DEFAULT_DIALECT]
    con, report = core.connect_ex(db_path, prelude=dial.prelude)
    state = {"width": 40, "con": con, "db_path": db_path, "dialect": dial,
             "db": catalog.current_database(con)}
    tables = core.list_tables(con)

    print(f"SQLPad REPL — \033[35m{dial.title}\033[0m on DuckDB.  "
          ".help for commands, .quit to leave.")
    if report is not None and (report.loaded or report.created or report.errors):
        print_sync(report)
    print(f"active database: \033[36m{state['db']}\033[0m  "
          "(.databases to list, .use <db> to switch)")
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
            prompt = f"[{state['db']}] {state['dialect'].prompt}"
            try:
                if session is not None:
                    line = session.prompt(prompt)
                else:
                    line = input(prompt)
            except EOFError:
                break

            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("."):
                if not _handle_meta(stripped, state):
                    break
                _track_db(state)
                continue

            _run_sql(state["con"], line, state["width"])
            _track_db(state)
    except KeyboardInterrupt:
        pass
    finally:
        state["con"].close()
        print("\nbye.")
