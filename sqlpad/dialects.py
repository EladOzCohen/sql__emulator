"""SQL dialect profiles.

SQLPad always runs on DuckDB underneath. A *dialect* adds a compatibility
"prelude" of TEMP macros so that syntax from MySQL or BigQuery runs unchanged,
plus a cheat-sheet of the differences that can't be papered over (those are
parser-level and no macro can fix them).

TEMP macros live only for the session and vanish on disconnect, so switching
dialects never contaminates your saved tables or another dialect.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Dialect:
    key: str
    title: str
    prompt: str
    scratch: str
    prelude: list[str] = field(default_factory=list)
    cheatsheet: str = ""


# --------------------------------------------------------------------------- #
#  Standard / DuckDB — no translation, the engine's own friendly SQL
# --------------------------------------------------------------------------- #
_STANDARD = Dialect(
    key="standard",
    title="Standard / DuckDB",
    prompt="sql> ",
    scratch="query.sql",
    prelude=[],
    cheatsheet=(
        "Standard / DuckDB — the engine's native SQL. Friendly extras you can use:\n"
        "  SELECT * EXCLUDE (col)         drop columns from *\n"
        "  SELECT * REPLACE (x+1 AS x)    transform a column in *\n"
        "  GROUP BY ALL / ORDER BY ALL    group/order by every non-aggregate\n"
        "  QUALIFY <window predicate>     filter on window functions\n"
        "  SELECT 1 a, 2 b,              trailing commas are fine\n"
    ),
)


# --------------------------------------------------------------------------- #
#  MySQL-flavored
# --------------------------------------------------------------------------- #
_MYSQL = Dialect(
    key="mysql",
    title="MySQL-flavored",
    prompt="mysql> ",
    scratch="query_mysql.sql",
    prelude=[
        # DATE_FORMAT(date, fmt) / STR_TO_DATE(str, fmt)
        "CREATE OR REPLACE TEMP MACRO date_format(d, fmt) AS strftime(d, fmt)",
        "CREATE OR REPLACE TEMP MACRO str_to_date(s, fmt) AS strptime(s, fmt)",
        # MySQL IFNULL exists natively in DuckDB; add NVL-style alias too.
        "CREATE OR REPLACE TEMP MACRO ifnull(x, y) AS coalesce(x, y)",
    ],
    cheatsheet=(
        "MySQL-flavored (runs on DuckDB). Provided via the prelude:\n"
        "  DATE_FORMAT(d,'%Y-%m')   STR_TO_DATE(s,'%Y-%m-%d')   IFNULL(x,y)\n"
        "\n"
        "Differences to keep in mind:\n"
        "  `col` backticks      ->  \"col\" double quotes\n"
        "  LIMIT off, cnt       ->  LIMIT cnt OFFSET off\n"
        "  GROUP_CONCAT(x)      ->  string_agg(x, ',')\n"
        "  x DIV y  /  x % y    ->  x // y   /   x % y\n"
        "  DATE_FORMAT minutes use %M here (DuckDB strftime), not %i\n"
    ),
)


# --------------------------------------------------------------------------- #
#  BigQuery (GoogleSQL)
# --------------------------------------------------------------------------- #
_BIGQUERY = Dialect(
    key="bigquery",
    title="BigQuery (GoogleSQL)",
    prompt="bq> ",
    scratch="query_bigquery.sql",
    prelude=[
        # SAFE_* arithmetic (return NULL instead of error; division is float)
        "CREATE OR REPLACE TEMP MACRO safe_divide(x, y) AS "
        "(CASE WHEN y = 0 THEN NULL ELSE (x * 1.0) / y END)",
        "CREATE OR REPLACE TEMP MACRO safe_add(x, y) AS (x + y)",
        "CREATE OR REPLACE TEMP MACRO safe_subtract(x, y) AS (x - y)",
        "CREATE OR REPLACE TEMP MACRO safe_multiply(x, y) AS (x * y)",
        "CREATE OR REPLACE TEMP MACRO safe_negate(x) AS (-x)",
        # Date/time formatting (GoogleSQL puts the format string first)
        "CREATE OR REPLACE TEMP MACRO format_date(fmt, d) AS strftime(d, fmt)",
        "CREATE OR REPLACE TEMP MACRO parse_date(fmt, s) AS strptime(s, fmt)::DATE",
        "CREATE OR REPLACE TEMP MACRO format_timestamp(fmt, t) AS strftime(t, fmt)",
        "CREATE OR REPLACE TEMP MACRO parse_timestamp(fmt, s) AS strptime(s, fmt)",
        "CREATE OR REPLACE TEMP MACRO format_datetime(fmt, t) AS strftime(t, fmt)",
        "CREATE OR REPLACE TEMP MACRO parse_datetime(fmt, s) AS strptime(s, fmt)",
        # Strings / arrays
        "CREATE OR REPLACE TEMP MACRO regexp_contains(s, pattern) AS regexp_matches(s, pattern)",
        "CREATE OR REPLACE TEMP MACRO split(s, delim) AS string_split(s, delim)",
        "CREATE OR REPLACE TEMP MACRO generate_array(a, b) AS generate_series(a, b)",
    ],
    cheatsheet=(
        "BigQuery / GoogleSQL (runs on DuckDB). Works natively — write it as-is:\n"
        "  QUALIFY, GROUP BY alias, GROUP BY ALL, trailing commas\n"
        "  COUNT_IF, APPROX_COUNT_DISTINCT, LOGICAL_AND/OR (as bool_and/bool_or)\n"
        "  STARTS_WITH, ENDS_WITH, REGEXP_EXTRACT, STRING_AGG, ARRAY_AGG, UNNEST\n"
        "Provided via the prelude:\n"
        "  SAFE_DIVIDE / SAFE_ADD / SAFE_SUBTRACT / SAFE_MULTIPLY / SAFE_NEGATE\n"
        "  FORMAT_DATE / PARSE_DATE / FORMAT_TIMESTAMP / PARSE_TIMESTAMP\n"
        "  REGEXP_CONTAINS, SPLIT, GENERATE_ARRAY\n"
        "\n"
        "Parser-level differences no macro can fix — translate these by hand:\n"
        "  SELECT * EXCEPT(col)        ->  SELECT * EXCLUDE (col)\n"
        "  SAFE_CAST(x AS INT64)       ->  TRY_CAST(x AS BIGINT)\n"
        "  DATE_TRUNC(d, MONTH)        ->  date_trunc('month', d)\n"
        "  DATE_DIFF(d1, d2, DAY)      ->  date_diff('day', d2, d1)   (note order)\n"
        "  STRUCT(1 AS a, 2 AS b)      ->  {'a': 1, 'b': 2}\n"
        "  UNNEST(arr) AS x            ->  UNNEST(arr) AS t(x)\n"
        "  COUNTIF(cond)               ->  count_if(cond)  (underscore)\n"
        "  SAFE.FN(...) prefix         ->  not supported; use the plain function\n"
        "  INT64/FLOAT64/STRING types  ->  BIGINT/DOUBLE/VARCHAR\n"
    ),
)


DIALECTS: dict[str, Dialect] = {
    d.key: d for d in (_STANDARD, _MYSQL, _BIGQUERY)
}

# friendly aliases accepted on the command line / menu
_ALIASES = {
    "duckdb": "standard",
    "std": "standard",
    "default": "standard",
    "my": "mysql",
    "sql": "standard",
    "bq": "bigquery",
    "google": "bigquery",
    "googlesql": "bigquery",
}

DEFAULT_DIALECT = "standard"


def resolve(key: str | None) -> Dialect:
    """Map a user-supplied key/alias to a Dialect (defaults to standard)."""
    if not key:
        return DIALECTS[DEFAULT_DIALECT]
    norm = key.strip().lower()
    norm = _ALIASES.get(norm, norm)
    if norm not in DIALECTS:
        raise ValueError(
            f"unknown dialect '{key}'. choose: "
            + ", ".join(DIALECTS) + " (or aliases: bq, duckdb, my)"
        )
    return DIALECTS[norm]
