"""Live .sql file runner.

Edit query.sql in any editor. Every time you save, SQLPad re-runs the whole
file and prints the results here. This is the whole point: write SQL like
normal SQL, not like a Python string.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from . import core, render

BANNER = r"""
  ___  ___  _    ___         _
 / __|/ _ \| |  | _ \__ _ __| |
 \__ \ (_) | |__|  _/ _` / _` |
 |___/\__\_\____|_| \__,_\__,_|   live .sql watcher
"""


def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _print_result(res: core.Result, index: int, total: int) -> None:
    head = render.strip_comments_head(res.statement)
    preview = " ".join(res.statement.split())
    if len(preview) > 90:
        preview = preview[:89] + "…"

    print(f"\n\033[36m[{index}/{total}] {preview}\033[0m")

    if not res.ok:
        print(f"  \033[31m✗ Error:\033[0m {res.error}")
        return

    if res.has_rows:
        print(render.render_table(res.columns, res.rows))
        print(f"  \033[90m{res.elapsed_ms:.1f} ms\033[0m")
    else:
        # DDL / DML that returns nothing
        verb = head.split()[0].upper() if head.split() else "OK"
        print(f"  \033[32m✓ {verb} ok\033[0m  \033[90m{res.elapsed_ms:.1f} ms\033[0m")


def run_once(con, sql_path: str) -> None:
    _clear()
    print(BANNER)
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"\033[90m{os.path.abspath(sql_path)}  ·  ran at {stamp}\033[0m")

    with open(sql_path, "r", encoding="utf-8") as fh:
        sql = fh.read()

    statements = render.split_statements(sql)
    if not statements:
        print("\n  (query.sql is empty — write some SQL and save)")
        return

    total = len(statements)
    for i, stmt in enumerate(statements, start=1):
        res = core.run_statement(con, stmt)
        _print_result(res, i, total)
        if not res.ok:
            # stop on first error, like a script would
            print("\n\033[90m  (stopped at first error)\033[0m")
            break


def watch(sql_path: str = "query.sql", db_path: str = core.DEFAULT_DB,
          poll: float = 0.4) -> None:
    """Watch sql_path and re-run on every save. Ctrl-C to quit."""
    # make sure the file exists so the user has something to edit
    if not os.path.exists(sql_path):
        with open(sql_path, "w", encoding="utf-8") as fh:
            fh.write("-- Write SQL here and save. Results appear in the watcher.\n"
                     "-- Try:\nSELECT 'hello, SQL' AS greeting;\n")

    con = core.connect(db_path)
    last_mtime = -1.0

    try:
        while True:
            try:
                mtime = os.path.getmtime(sql_path)
            except OSError:
                time.sleep(poll)
                continue
            if mtime != last_mtime:
                last_mtime = mtime
                try:
                    run_once(con, sql_path)
                except Exception as exc:  # noqa: BLE001
                    print(f"\n\033[31mUnexpected error:\033[0m {exc}")
                print("\n\033[90mWatching for changes… (edit + save query.sql, "
                      "Ctrl-C to quit)\033[0m")
            time.sleep(poll)
    except KeyboardInterrupt:
        print("\n\033[90mbye.\033[0m")
    finally:
        con.close()
