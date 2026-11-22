"""Dialect compatibility QA: run documented examples through a dialect.

Each case in tests/compat/cases/<dialect>_*.json is a SELECT taken from the
official MySQL / BigQuery manual together with the result the manual shows.
This runner executes every case through SQLPad's dialect layer (session
settings, macros, translation) and compares with the documented result.

    python tests/compat/run_compat.py --dialect mysql
    python tests/compat/run_compat.py --dialect mysql --fn DATE_SUB -v
    python tests/compat/run_compat.py --dialect bigquery --file bigquery_string

Exit code is 0 only when nothing failed. Cases the manual itself marks as
errors / environment dependent are counted as "skipped", never as passed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import glob
import json
import math
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from sqlpad import core, dialects, render  # noqa: E402

CASES_DIR = os.path.join(ROOT, "tests", "compat", "cases")
_NOT_CASE_FILES = ("functions", "specifiers", "format_elements")


def load_skips() -> dict:
    """id -> reason, from every tests/compat/known_skips*.json.

    A skip is only for a case that cannot be judged (time zone, the manual
    contradicts itself) or that DuckDB cannot represent; each needs a reason.
    """
    skips: dict = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "tests", "compat", "known_skips*.json"))):
        with open(path, encoding="utf-8") as fh:
            skips.update(json.load(fh))
    return skips


SKIPS = load_skips()


# --------------------------------------------------------------------------- #
#  Value formatting / comparison
# --------------------------------------------------------------------------- #
def fmt(value) -> str:
    """Render an engine value the way the manuals print results."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, decimal.Decimal)):
        return str(value)
    if isinstance(value, dt.datetime):
        text = value.replace(tzinfo=None).isoformat(sep=" ")
        if value.tzinfo is not None:
            off = value.utcoffset() or dt.timedelta(0)
            mins = int(off.total_seconds() // 60)
            sign = "+" if mins >= 0 else "-"
            text += f"{sign}{abs(mins) // 60:02d}" + (f":{abs(mins) % 60:02d}" if abs(mins) % 60 else "")
        return text
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        total = int(value.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        text = f"{sign}{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"
        if value.microseconds:
            text += f".{value.microseconds:06d}"
        return text
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).hex().upper()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(fmt(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {fmt(v)}" for k, v in value.items()) + "}"
    return str(value)


def _squash(text: str) -> str:
    return re.sub(r"[\s'\"`]", "", text)


def _try_float(text: str):
    try:
        return float(text)
    except ValueError:
        return None


def equal(expected: str, actual_value) -> bool:
    actual = fmt(actual_value)
    if expected == actual:
        return True
    e, a = expected.strip(), actual.strip()
    if e == a:
        return True
    if e.upper() == "NULL" or a == "NULL":
        return e.upper() == a.upper()
    # booleans: MySQL prints 1/0, GoogleSQL prints true/false
    if isinstance(actual_value, bool):
        return e.lower() in (("true", "1") if actual_value else ("false", "0"))
    fe, fa = _try_float(e), _try_float(a)
    if fe is not None and fa is not None:
        return math.isclose(fe, fa, rel_tol=1e-9, abs_tol=1e-9)
    # timestamps: "+00" vs "+00:00" vs " UTC"
    norm = lambda s: re.sub(r"(\+00(:00)?| UTC)$", "+00", s)  # noqa: E731
    if norm(e) == norm(a):
        return True
    # containers: ignore quoting/whitespace differences
    if e[:1] in "[{(" and _squash(e) == _squash(a):
        return True
    return False


def rows_equal(expected_rows, rows) -> bool:
    if len(expected_rows) != len(rows):
        return False
    for erow, row in zip(expected_rows, rows):
        if len(erow) != len(row):
            return False
        if not all(equal(e, v) for e, v in zip(erow, row)):
            return False
    return True


# --------------------------------------------------------------------------- #
#  Running
# --------------------------------------------------------------------------- #
def load_cases(dialect_key: str, only_file: str | None):
    prefix = {"mysql": "mysql_", "bigquery": "bigquery_"}[dialect_key]
    cases = []
    for path in sorted(glob.glob(os.path.join(CASES_DIR, prefix + "*.json"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        if any(stem.endswith(s) for s in _NOT_CASE_FILES):
            continue
        if only_file and only_file not in stem:
            continue
        with open(path, encoding="utf-8") as fh:
            for case in json.load(fh):
                case["_file"] = stem
                cases.append(case)
    return cases


def run_case(con, dialect, case):
    """Return (status, detail). status: pass | fail | error | skip."""
    if case["id"] in SKIPS:
        return "skip", SKIPS[case["id"]]
    if case.get("expected_rows") is None:
        return "skip", "documented as error/warning/environment dependent"
    res = None
    for stmt in render.split_statements(case["sql"]):
        res = core.run_statement(con, stmt, dialect)
        if not res.ok:
            return "error", res.error.splitlines()[0][:160] + (
                f"  [ran as: {' '.join(res.translated.split())[:140]}]" if res.translated else "")
    if res is None or not res.has_rows:
        return "fail", "no result set"
    if rows_equal(case["expected_rows"], res.rows):
        return "pass", ""
    got = [[fmt(v) for v in row] for row in res.rows]
    return "fail", f"expected {case['expected_rows']} got {got}" + (
        f"  [ran as: {' '.join(res.translated.split())[:140]}]" if res.translated else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dialect", required=True, choices=["mysql", "bigquery"])
    ap.add_argument("--file", help="only case files whose name contains this")
    ap.add_argument("--fn", help="only cases for this function name (case-insensitive)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show every failing case")
    ap.add_argument("--json", help="write detailed results to this path")
    args = ap.parse_args(argv)

    dialect = dialects.resolve(args.dialect)
    con = core.connect(":memory:", dialect=dialect, databases=False)
    cases = load_cases(args.dialect, args.file)
    if args.fn:
        cases = [c for c in cases if c["function"].upper() == args.fn.upper()]

    tally = defaultdict(lambda: defaultdict(int))
    results = []
    for case in cases:
        status, detail = run_case(con, dialect, case)
        tally[case["function"].upper()][status] += 1
        results.append({"id": case["id"], "function": case["function"], "status": status,
                        "sql": case["sql"], "detail": detail, "notes": case.get("notes", "")})
        if status in ("fail", "error") and args.verbose:
            print(f"{status.upper():5} {case['id']} {case['function']}\n      {' '.join(case['sql'].split())[:200]}\n      {detail}")

    totals = defaultdict(int)
    for fn_counts in tally.values():
        for status, n in fn_counts.items():
            totals[status] += n
    print(f"\n{args.dialect}: {len(cases)} cases  "
          f"pass={totals['pass']} fail={totals['fail']} error={totals['error']} skip={totals['skip']}")
    bad = {fn: c for fn, c in tally.items() if c["fail"] or c["error"]}
    if bad:
        print("functions with problems:")
        for fn, c in sorted(bad.items()):
            print(f"  {fn:22} pass={c['pass']} fail={c['fail']} error={c['error']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=1, ensure_ascii=False)
    con.close()
    return 0 if not (totals["fail"] or totals["error"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
