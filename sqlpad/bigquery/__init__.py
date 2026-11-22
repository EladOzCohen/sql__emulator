"""BigQuery (GoogleSQL) dialect for SQLPad: translation + functions on top of DuckDB.

Layout (each module contributes a PRELUDE of TEMP macros, call HANDLERS,
token-level PRE_RULES / POST_RULES and a register(con) for Python functions):

    temporal   DATE / DATETIME / TIMESTAMP / TIME functions, format elements
    strings    string functions, REGEXP_*, FORMAT
    arrays     arrays, STRUCT, UNNEST, GENERATE_*_ARRAY
    mathconv   math, conditional expressions, CAST / SAFE_CAST and types
    aggregate  aggregates, window functions, operators
    rules      clause rewrites (backticks with dots, * EXCEPT, SAFE. prefix)

The compatibility suite in tests/compat runs the GoogleSQL reference's own
examples against this package.
"""

from __future__ import annotations

import importlib
import sys

from ..translate import Translator

# BigQuery defaults that DuckDB exposes as settings
SESSION = [
    "SET TimeZone = 'UTC'",                                        # TIMESTAMP is UTC
    "SET default_null_order = 'nulls_first_on_asc_last_on_desc'",  # NULL sorts lowest
]

_MODULE_NAMES = ("temporal", "strings", "arrays", "mathconv", "aggregate")


def _load(name: str):
    """Import a feature module; a broken one is reported but never takes the rest down."""
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except Exception as exc:  # noqa: BLE001
        print(f"sqlpad.bigquery: module '{name}' failed to load: {exc!r}", file=sys.stderr)
        return None


from . import rules  # noqa: E402

MODULES = tuple(m for m in (_load(n) for n in _MODULE_NAMES) if m is not None)


def build_translator() -> Translator:
    t = Translator(backslash_escapes=True, hash_comments=True,
                   unnamed_style="fN", bq_strings=True)
    t.pre_rules.extend(rules.PRE_RULES)
    t.post_rules.extend(rules.POST_RULES)
    for m in MODULES:
        t.add_handlers(m.HANDLERS)
        t.pre_rules.extend(m.PRE_RULES)
        t.post_rules.extend(m.POST_RULES)
    return t


def prelude() -> list[str]:
    out: list[str] = []
    for m in MODULES:
        out.extend(m.PRELUDE)
    return out


def register(con) -> None:
    for m in MODULES:
        try:
            m.register(con)
        except Exception as exc:  # noqa: BLE001
            print(f"sqlpad.bigquery: {m.__name__}.register failed: {exc!r}", file=sys.stderr)
