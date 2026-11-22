"""Which documented functions does a dialect not know at all?

Reads tests/compat/cases/<dialect>_*functions*.json (every function name the
official manual lists) and checks that each name resolves to a DuckDB function,
a dialect macro / Python function, or a translator handler. Names that only
exist as syntax (AND, CASE, IN ...) are reported separately: the example cases
cover those.

    python tests/compat/audit_functions.py --dialect mysql
    python tests/compat/audit_functions.py --dialect bigquery --show-ok
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from sqlpad import core, dialects  # noqa: E402

CASES_DIR = os.path.join(ROOT, "tests", "compat", "cases")
SYNTAX_ONLY = {
    "AND", "OR", "NOT", "XOR", "IN", "IS", "LIKE", "BETWEEN", "CASE", "ESCAPE", "EXISTS",
    "ALL", "ANY", "SOME", "DISTINCT", "NULL", "TRUE", "FALSE", "DIV", "MOD", "REGEXP",
    "RLIKE", "SOUNDS", "INTERVAL", "BINARY", "COLLATE", "OVER", "WINDOW", "UNNEST",
}


def documented_names(dialect_key: str) -> dict[str, str]:
    """name -> source file, from the *_functions.json lists."""
    names: dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(CASES_DIR, f"{dialect_key}_*functions*.json"))):
        with open(path, encoding="utf-8") as fh:
            for entry in json.load(fh):
                names.setdefault(entry["name"].strip(), os.path.basename(path))
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dialect", required=True, choices=["mysql", "bigquery"])
    ap.add_argument("--show-ok", action="store_true", help="also list names that resolve")
    args = ap.parse_args()

    dialect = dialects.resolve(args.dialect)
    con = core.connect(":memory:", dialect=dialect, databases=False)
    available = {r[0].lower() for r in con.execute(
        "select distinct function_name from duckdb_functions()").fetchall()}
    handlers = {h.lower() for h in getattr(dialect.translate, "handlers", {})}

    def known_to_engine(base: str) -> bool:
        """Parser-level functions (CAST, COALESCE, IF ...) are not in duckdb_functions():
        probe with a call and treat anything but 'does not exist' as existing."""
        try:
            con.execute(f"SELECT {base}()")
            return True
        except Exception as exc:  # noqa: BLE001
            return "does not exist" not in str(exc)

    missing, syntax, ok = [], [], []
    for name, src in documented_names(args.dialect).items():
        base = name.split("(")[0].strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", base):
            syntax.append(name)                     # operators such as +, <=>, ||
            continue
        key = base.lower().split(".")[-1]
        if base.upper() in SYNTAX_ONLY:
            syntax.append(name)
        elif key in available or key in handlers or known_to_engine(base):
            ok.append(name)
        else:
            missing.append(name)

    print(f"{args.dialect}: {len(ok)} documented names resolve, "
          f"{len(missing)} are MISSING, {len(syntax)} are operators/syntax (covered by cases)")
    if missing:
        print("missing:", ", ".join(sorted(missing, key=str.lower)))
    if args.show_ok:
        print("ok:", ", ".join(sorted(ok, key=str.lower)))
    con.close()
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
