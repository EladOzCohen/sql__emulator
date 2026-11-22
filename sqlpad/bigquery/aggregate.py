"""BigQuery aggregate functions on DuckDB (to be filled by the QA pass; see tests/compat)."""

from __future__ import annotations

from . import helpers as H  # noqa: F401

PRELUDE: list[str] = []
HANDLERS: dict = {}
PRE_RULES: list = []
POST_RULES: list = []


def register(con) -> None:
    pass
