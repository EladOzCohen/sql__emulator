"""Shared helpers for the MySQL dialect modules."""

from __future__ import annotations

from typing import Callable

try:                                    # DuckDB >= 1.4
    from duckdb import sqltypes as T
except ImportError:                     # older DuckDB
    from duckdb import typing as T      # type: ignore[no-redef]

VARCHAR, BIGINT, INTEGER, DOUBLE = T.VARCHAR, T.BIGINT, T.INTEGER, T.DOUBLE
DATE, TIMESTAMP, BOOLEAN = T.DATE, T.TIMESTAMP, T.BOOLEAN


def udf(con, name: str, fn: Callable, params: list, ret) -> None:
    """Register a Python function as a SQL function.

    `fn` is called even for NULL arguments (it decides what NULL means) and may
    return None; an exception inside `fn` returns NULL instead of aborting the
    query (MySQL answers NULL + warning in those cases).
    """
    con.create_function(name, fn, params, ret, null_handling="special",
                        exception_handling="return_null")


def macro(name: str, *definitions: str) -> str:
    """CREATE TEMP MACRO name with one or more overloads.

    Each definition is "(params) AS body", e.g. "(x) AS upper(x)".
    """
    return f"CREATE OR REPLACE TEMP MACRO {name}" + ", ".join(
        d if i == 0 else d for i, d in enumerate(definitions))
