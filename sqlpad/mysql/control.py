"""MySQL control-flow, comparison and cast helpers on DuckDB.

Most of IF / IFNULL / NULLIF / COALESCE / CASE / GREATEST / LEAST already match
DuckDB. This module adds what is missing: ISNULL() and the INTERVAL(N, N1, ...)
function (not the date INTERVAL keyword, which temporal.py handles).

Known gaps (documented, see tests/compat/known_skips*.json): MySQL converts
text <-> number implicitly in comparisons and IFNULL/BETWEEN/IN with mixed types,
and has CAST(... AS UNSIGNED / YEAR); DuckDB is strict about those.
"""

from __future__ import annotations

from .. import translate as tr
from . import helpers as H  # noqa: F401


def _h_interval(args: list[tr.Arg]):
    """INTERVAL(N, N1, N2, ...): index of the first Ni greater than N (-1 for NULL)."""
    if len(args) < 3:
        return None                      # INTERVAL (expr) UNIT is date syntax, not this
    n = args[0].text
    whens = " ".join(f"WHEN ({n}) < ({a.text}) THEN {i}" for i, a in enumerate(args[1:]))
    return f"(CASE WHEN ({n}) IS NULL THEN -1 {whens} ELSE {len(args) - 1} END)"


def _h_isnull(args: list[tr.Arg]):
    # ISNULL is a keyword in DuckDB, so it cannot be a macro
    return f"(CASE WHEN ({args[0].text}) IS NULL THEN 1 ELSE 0 END)" if len(args) == 1 else None


PRELUDE: list[str] = []
HANDLERS: dict = {"INTERVAL": _h_interval, "ISNULL": _h_isnull}
PRE_RULES: list = []
POST_RULES: list = []


def register(con) -> None:
    pass
