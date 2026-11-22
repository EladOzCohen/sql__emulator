"""MySQL client / administration statements: SHOW ..., DESCRIBE, DATABASE().

DuckDB has similar commands that return different column names and values, so
the statement is rewritten to a query over information_schema that returns the
columns MySQL would: `Tables_in_<db>`, `Field | Type | Null | Key | Default |
Extra`, `Database`, `Table | Create Table`.
"""

from __future__ import annotations

import re

from .. import translate as tr

_CON = None          # the connection of the current session (set by register)


def register(con) -> None:
    global _CON
    _CON = con


def _current_db() -> str:
    try:
        return _CON.execute("select current_database()").fetchone()[0]
    except Exception:  # noqa: BLE001
        return "main"


def _lit(s: str) -> str:
    return tr.quote_string(s)


def _words(toks: list[tr.Tok]) -> list[tr.Tok]:
    return [t for t in toks if t.kind not in (tr.WS, tr.COMMENT)]


def _name_parts(sig: list[tr.Tok]) -> list[str] | None:
    """`a`, `a.b` identifiers -> ['a', 'b'] (unquoted); None if it is anything else."""
    parts: list[str] = []
    expect_name = True
    for t in sig:
        if expect_name:
            if t.kind != tr.WORD:
                return None
            parts.append(t.value if t.value is not None else t.text.strip('"'))
        elif not t.is_punct("."):
            return None
        expect_name = not expect_name
    return parts if parts and not expect_name else None


_TYPE_MAP = (
    "CASE upper(data_type) "
    "WHEN 'INTEGER' THEN 'int' WHEN 'BIGINT' THEN 'bigint' WHEN 'SMALLINT' THEN 'smallint' "
    "WHEN 'TINYINT' THEN 'tinyint' WHEN 'DOUBLE' THEN 'double' WHEN 'FLOAT' THEN 'float' "
    "WHEN 'VARCHAR' THEN 'text' WHEN 'TIMESTAMP' THEN 'datetime' WHEN 'BOOLEAN' THEN 'tinyint(1)' "
    "ELSE lower(data_type) END"
)


def _show_tables(rest: list[tr.Tok], full: bool) -> str | None:
    db = None
    like = None
    i = 0
    if rest and rest[0].is_word("FROM", "IN"):
        if len(rest) < 2:
            return None
        parts = _name_parts(rest[1:2])
        if not parts:
            return None
        db, i = parts[0], 2
    if i < len(rest):
        if rest[i].is_word("LIKE") and i + 1 < len(rest) and rest[i + 1].kind == tr.STR:
            like = rest[i + 1].value
            i += 2
    if i != len(rest):
        return None
    db = db or _current_db()
    cols = f'table_name AS "Tables_in_{db}"'
    if full:
        cols += ", CASE table_type WHEN 'VIEW' THEN 'VIEW' ELSE 'BASE TABLE' END AS \"Table_type\""
    where = f"table_catalog = {_lit(db)} AND table_schema = 'main'"
    if like is not None:
        where += f" AND table_name ILIKE {_lit(like)} ESCAPE '\\'"
    return (f"SELECT {cols} FROM information_schema.tables WHERE {where} "
            f"ORDER BY table_name")


def _describe(parts: list[str]) -> str:
    db, table = (parts[0], parts[1]) if len(parts) >= 2 else (_current_db(), parts[0])
    return (
        f'SELECT column_name AS "Field", {_TYPE_MAP} AS "Type", '
        f"CASE WHEN is_nullable = 'YES' THEN 'YES' ELSE 'NO' END AS \"Null\", "
        f"'' AS \"Key\", column_default AS \"Default\", '' AS \"Extra\" "
        f"FROM information_schema.columns "
        f"WHERE table_catalog = {_lit(db)} AND table_schema = 'main' AND table_name = {_lit(table)} "
        f"ORDER BY ordinal_position")


def _show_databases() -> str:
    return ('SELECT database_name AS "Database" FROM duckdb_databases() '
            'WHERE NOT internal ORDER BY database_name')


def _show_create(parts: list[str]) -> str:
    db, table = (parts[0], parts[1]) if len(parts) >= 2 else (_current_db(), parts[0])
    return (f'SELECT table_name AS "Table", sql AS "Create Table" FROM duckdb_tables() '
            f"WHERE database_name = {_lit(db)} AND table_name = {_lit(table)}")


def rule_admin(toks: list[tr.Tok]) -> list[tr.Tok]:
    """Rewrite SHOW TABLES / SHOW DATABASES / DESCRIBE / SHOW CREATE TABLE statements."""
    sig = _words(toks)
    if not sig or sig[0].kind != tr.WORD:
        return toks
    head = sig[0].upper
    out: str | None = None
    if head == "SHOW" and len(sig) >= 2:
        second = sig[1].upper if sig[1].kind == tr.WORD else ""
        if second == "TABLES":
            out = _show_tables(sig[2:], full=False)
        elif second == "FULL" and len(sig) >= 3 and sig[2].is_word("TABLES"):
            out = _show_tables(sig[3:], full=True)
        elif second in ("DATABASES", "SCHEMAS") and len(sig) == 2:
            out = _show_databases()
        elif second in ("COLUMNS", "FIELDS") and len(sig) >= 4 and sig[2].is_word("FROM", "IN"):
            parts = _name_parts(sig[3:])
            out = _describe(parts) if parts else None
        elif (second == "CREATE" and len(sig) >= 4 and sig[2].is_word("TABLE")):
            parts = _name_parts(sig[3:])
            out = _show_create(parts) if parts else None
    elif head in ("DESCRIBE", "DESC") and len(sig) >= 2:
        if not sig[1].is_word("SELECT", "WITH", "TABLE", "FROM"):
            parts = _name_parts(sig[1:])
            out = _describe(parts) if parts else None
    if out is None:
        return toks
    return tr.tokenize_plain(out)


PRELUDE = [
    "CREATE OR REPLACE TEMP MACRO database() AS current_database()",
    "CREATE OR REPLACE TEMP MACRO schema() AS current_database()",
    "CREATE OR REPLACE TEMP MACRO connection_id() AS 1",
]
HANDLERS: dict = {}
PRE_RULES = [rule_admin]
POST_RULES: list = []
