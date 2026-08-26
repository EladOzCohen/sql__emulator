"""Database plumbing: connect, import CSV, run statements."""

from __future__ import annotations

import os
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


def connect(db_path: str = DEFAULT_DB) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) a persistent DuckDB database file."""
    con = duckdb.connect(db_path)
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


def list_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute("SHOW TABLES").fetchall()
    return [r[0] for r in rows]


def table_schema(con: duckdb.DuckDBPyConnection, table: str) -> Result:
    return run_statement(con, f"DESCRIBE {table}")
