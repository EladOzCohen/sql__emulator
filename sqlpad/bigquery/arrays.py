"""BigQuery arrays functions on DuckDB (to be filled by the QA pass; see tests/compat)."""

from __future__ import annotations

from . import helpers as H  # noqa: F401

# starting point carried over from the first version of the dialect
PRELUDE: list[str] = [
    "CREATE OR REPLACE TEMP MACRO generate_array(a, b) AS generate_series(a, b)",
]
HANDLERS: dict = {}
PRE_RULES: list = []
POST_RULES: list = []


def register(con) -> None:
    pass
