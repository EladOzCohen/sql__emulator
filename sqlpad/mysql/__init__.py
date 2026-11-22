"""MySQL dialect for SQLPad: translation + functions on top of DuckDB.

Layout (each module contributes a PRELUDE of TEMP macros, call HANDLERS,
token-level PRE_RULES / POST_RULES and a register(con) for Python functions):

    temporal   date and time functions, INTERVAL arithmetic
    strings    string functions, LIKE / REGEXP
    numeric    arithmetic, math functions, DIV / MOD
    control    IF / IFNULL / CASE, comparison, CAST / CONVERT, types
    aggregate  GROUP_CONCAT, aggregates, window functions, misc
    rules      clause rewrites (LIMIT a, b; LIKE; <=>; FROM DUAL ...)

The compatibility suite in tests/compat runs the MySQL manual's own examples
against this package.
"""

from __future__ import annotations

import importlib
import sys

from ..translate import Translator

# MySQL defaults that DuckDB exposes as settings
SESSION = [
    "SET default_collation = 'nocase'",             # case-insensitive text comparison
    "SET ieee_floating_point_ops = false",          # 1/0 -> NULL (not inf)
    "SET default_null_order = 'nulls_first_on_asc_last_on_desc'",   # NULL sorts lowest
]

_MODULE_NAMES = ("temporal", "strings", "numeric", "control", "aggregate", "admin")


def _load(name: str):
    """Import a feature module; a broken one is reported but never takes the rest down."""
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except Exception as exc:  # noqa: BLE001
        print(f"sqlpad.mysql: module '{name}' failed to load: {exc!r}", file=sys.stderr)
        return None


from . import rules  # noqa: E402  (plain module, no dependencies on the others)

MODULES = tuple(m for m in (_load(n) for n in _MODULE_NAMES) if m is not None)


def build_translator() -> Translator:
    t = Translator(backslash_escapes=True, hash_comments=True)
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
            print(f"sqlpad.mysql: {m.__name__}.register failed: {exc!r}", file=sys.stderr)
