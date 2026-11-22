"""BigQuery mathconv functions on DuckDB (to be filled by the QA pass; see tests/compat)."""

from __future__ import annotations

from . import helpers as H  # noqa: F401

# starting point carried over from the first version of the dialect
PRELUDE: list[str] = [
    "CREATE OR REPLACE TEMP MACRO safe_divide(x, y) AS "
    "(CASE WHEN y = 0 THEN NULL ELSE (x * 1.0) / y END)",
    "CREATE OR REPLACE TEMP MACRO safe_add(x, y) AS (x + y)",
    "CREATE OR REPLACE TEMP MACRO safe_subtract(x, y) AS (x - y)",
    "CREATE OR REPLACE TEMP MACRO safe_multiply(x, y) AS (x * y)",
    "CREATE OR REPLACE TEMP MACRO safe_negate(x) AS (-x)",
]
HANDLERS: dict = {}
PRE_RULES: list = []
POST_RULES: list = []


def register(con) -> None:
    pass
