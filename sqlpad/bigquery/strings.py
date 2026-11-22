"""BigQuery strings functions on DuckDB (to be filled by the QA pass; see tests/compat)."""

from __future__ import annotations

from . import helpers as H  # noqa: F401

# starting point carried over from the first version of the dialect
PRELUDE: list[str] = [
    "CREATE OR REPLACE TEMP MACRO regexp_contains(s, pattern) AS regexp_matches(s, pattern)",
    "CREATE OR REPLACE TEMP MACRO split(s, delim) AS string_split(s, delim)",
]
HANDLERS: dict = {}
PRE_RULES: list = []
POST_RULES: list = []


def register(con) -> None:
    pass
