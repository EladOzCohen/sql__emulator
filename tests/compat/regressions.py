"""Queries that must keep working in every dialect (no manual cases needed).

Each one broke at some point; see the comment. Run:  python tests/compat/regressions.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from sqlpad import core, dialects  # noqa: E402

T = "(SELECT 1 AS n, 5 AS m UNION ALL SELECT 2, 7) t"
CASES = {
    "mysql": [
        # operator macros once broke every aggregate / window operand ("INTEGER -> BLOB")
        "SELECT SUM(n) + 1 FROM " + T,
        "SELECT SUM(n) / COUNT(*) FROM " + T,
        "SELECT n + LAG(n, 1, 0) OVER (ORDER BY n) FROM " + T,
        "SELECT SUM(n) OVER () / 2 FROM " + T,
        "SELECT ROUND(AVG(m), 1) FROM " + T,
        # a macro named round made DuckDB's round_even recurse forever
        "SELECT ROUND(-1.23), ROUND(2.5)",
        # the original report
        "select DATE_SUB('2024-05-30', INTERVAL 10 DAY)",
        "SELECT NOW() - INTERVAL 1 DAY < NOW()",
        "SELECT INTERVAL(23, 1, 15, 17, 30), ISNULL(1 + 1)",
        "SELECT `n` FROM " + T + " LIMIT 0, 1",
    ],
    "bigquery": [
        "SELECT SUM(n) + 1, SAFE_DIVIDE(SUM(m), COUNT(*)) FROM " + T,
        "SELECT * EXCEPT (m) FROM " + T,
    ],
    "standard": [
        "SELECT SUM(n) / COUNT(*) FROM " + T,
    ],
}


def main() -> int:
    bad = 0
    for key, queries in CASES.items():
        dialect = dialects.resolve(key)
        con = core.connect(":memory:", dialect=dialect, databases=False)
        for sql in queries:
            res = core.run_statement(con, sql, dialect)
            if not res.ok:
                bad += 1
                print(f"FAIL [{key}] {sql}\n     {res.error.splitlines()[0][:150]}")
        con.close()
    print("regressions:", "ALL OK" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
