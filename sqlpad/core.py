"""Database plumbing: connect, import CSV, run statements."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any

import duckdb


DEFAULT_DB = os.environ.get("SQLPAD_DB", "practice.duckdb")


@dataclass
class Result:
    """Outcome of running one SQL statement."""
    statement: str
    columns: list[str] | None      # None => statement returned no result set
    rows: list[tuple[Any, ...]]
    elapsed_ms: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def has_rows(self) -> bool:
        return self.columns is not None


def connect(
    db_path: str = DEFAULT_DB,
    prelude: list[str] | None = None,
) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) a persistent DuckDB database file.

    `prelude` is an optional list of statements (dialect TEMP macros) run
    immediately after connecting. They are session-only and never persisted.
    """
    con = duckdb.connect(db_path)
    for stmt in prelude or []:
        con.execute(stmt)
    return con


def run_statement(con: duckdb.DuckDBPyConnection, statement: str) -> Result:
    """Execute a single SQL statement and capture columns/rows/timing/error."""
    start = time.perf_counter()
    try:
        cur = con.execute(statement)
        elapsed = (time.perf_counter() - start) * 1000.0
        if cur.description:
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return Result(statement, columns, rows, elapsed)
        return Result(statement, None, [], elapsed)
    except Exception as exc:  # noqa: BLE001 - surface any engine error to the user
        elapsed = (time.perf_counter() - start) * 1000.0
        return Result(statement, None, [], elapsed, error=str(exc))


def import_csv(
    con: duckdb.DuckDBPyConnection,
    csv_path: str,
    table: str,
    replace: bool = True,
) -> int:
    """Load a CSV file into a persisted table. Returns the row count.

    DuckDB sniffs the delimiter, header and column types automatically.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(csv_path)
    verb = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE IF NOT EXISTS"
    # read_csv_auto handles delimiter/type sniffing; parameterise the path only.
    con.execute(
        f"{verb} {table} AS "
        "SELECT * FROM read_csv_auto(?, header=true, sample_size=-1)",
        [csv_path],
    )
    (count,) = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    return int(count)


def import_excel(
    con: duckdb.DuckDBPyConnection,
    xlsx_path: str,
    table: str,
    sheet: int | str = 0,
    replace: bool = True,
) -> int:
    """Load one sheet of an Excel file into a persisted table.

    Uses pandas (+openpyxl) so it works fully offline — no DuckDB extension
    download needed. `sheet` is a 0-based index or a sheet name.
    """
    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(xlsx_path)
    import pandas as pd  # lazy: keeps watcher/REPL startup light

    df = pd.read_excel(xlsx_path, sheet_name=sheet)
    verb = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE IF NOT EXISTS"
    con.register("__sqlpad_import_df", df)
    try:
        con.execute(f"{verb} {table} AS SELECT * FROM __sqlpad_import_df")
    finally:
        con.unregister("__sqlpad_import_df")
    (count,) = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    return int(count)


_EXCEL_EXT = {".xlsx", ".xlsm", ".xls", ".xlsb"}
_CSV_EXT = {".csv", ".tsv", ".txt", ""}


def import_file(
    con: duckdb.DuckDBPyConnection,
    path: str,
    table: str,
    sheet: int | str = 0,
    replace: bool = True,
) -> int:
    """Import a CSV or Excel file into a table, dispatching on file extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXCEL_EXT:
        return import_excel(con, path, table, sheet=sheet, replace=replace)
    if ext in _CSV_EXT:
        return import_csv(con, path, table, replace=replace)
    raise ValueError(
        f"unsupported file type '{ext}'. Use a CSV (.csv/.tsv) or Excel (.xlsx) file."
    )


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def load_sample_data(con: duckdb.DuckDBPyConnection, data_dir: str = DATA_DIR) -> list[str]:
    """Load the bundled practice tables from `data/` that are not in the db yet.

    practice.duckdb is gitignored, so a fresh checkout (or another PC) starts
    empty. Each CSV/Excel file becomes a table named after the file
    (emp.xlsx -> emp); each seed_*.sql script runs only if none of the tables
    it creates exist. Existing tables are never touched. Returns what loaded.
    """
    from .render import split_statements  # local: keep core importable alone

    if not os.path.isdir(data_dir):
        return []
    existing = {t.lower() for t in list_tables(con)}
    loaded: list[str] = []
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        stem, ext = os.path.splitext(name)
        ext = ext.lower()
        if ext in _EXCEL_EXT | {".csv", ".tsv"}:
            if stem.lower() not in existing:
                import_file(con, path, stem)
                loaded.append(stem)
        elif ext == ".sql" and stem.startswith("seed_"):
            with open(path, encoding="utf-8") as fh:
                script = fh.read()
            creates = {m.lower() for m in re.findall(
                r"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(\w+)", script, re.IGNORECASE)}
            if creates and not creates & existing:
                for stmt in split_statements(script):
                    con.execute(stmt)
                loaded.extend(sorted(creates))
    return loaded


def list_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute("SHOW TABLES").fetchall()
    return [r[0] for r in rows]


def table_schema(con: duckdb.DuckDBPyConnection, table: str) -> Result:
    return run_statement(con, f"DESCRIBE {table}")
