"""BigQuery temporal functions on DuckDB (to be filled by the QA pass; see tests/compat)."""

from __future__ import annotations

from . import helpers as H  # noqa: F401

# starting point carried over from the first version of the dialect
PRELUDE: list[str] = [
    "CREATE OR REPLACE TEMP MACRO format_date(fmt, d) AS strftime(d, fmt)",
    "CREATE OR REPLACE TEMP MACRO parse_date(fmt, s) AS strptime(s, fmt)::DATE",
    "CREATE OR REPLACE TEMP MACRO format_timestamp(fmt, t) AS strftime(t, fmt)",
    "CREATE OR REPLACE TEMP MACRO parse_timestamp(fmt, s) AS strptime(s, fmt)",
    "CREATE OR REPLACE TEMP MACRO format_datetime(fmt, t) AS strftime(t, fmt)",
    "CREATE OR REPLACE TEMP MACRO parse_datetime(fmt, s) AS strptime(s, fmt)",
]
HANDLERS: dict = {}
PRE_RULES: list = []
POST_RULES: list = []


def register(con) -> None:
    pass
