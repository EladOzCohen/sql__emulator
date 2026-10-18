"""Databases: every subfolder of data/ is a database, every CSV in it a table.

    data/
      sql_cookbook_csv/   ->  database "sql_cookbook_csv"
        emp.csv           ->    table  emp
        dept.csv          ->    table  dept

Each folder-backed database lives in its own DuckDB file under databases/ and is
ATTACHed on connect, so `USE <db>` (or the IDE tree) picks the active one and
`<db>.<table>` works across databases, like MSSQL. The default database
(practice.duckdb) holds tables you create by hand.

Sync rule: a CSV is (re)loaded only when it is new or its size/mtime changed
since the last load. Changes you make with UPDATE/DELETE/DROP are kept across
sessions; `reset()` rebuilds a database from its CSVs.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CSV_EXT = {".csv", ".tsv"}
_RESERVED = {"system", "temp", "memory", "main"}


# --------------------------------------------------------------------------- #
#  Locations & naming
# --------------------------------------------------------------------------- #
def data_dir() -> Path:
    return Path(os.environ.get("SQLPAD_DATA") or PROJECT_ROOT / "data")


def databases_dir() -> Path:
    return data_dir().parent / "databases"


def _state_path() -> Path:
    return databases_dir() / "_state.json"


def ident(name: str) -> str:
    """Turn a folder/file stem into a safe SQL identifier."""
    out = re.sub(r"[^0-9A-Za-z_]", "_", name.strip())
    if not out:
        out = "_"
    if out[0].isdigit():
        out = "_" + out
    return out


def qi(name: str) -> str:
    """Quote an identifier."""
    return '"' + name.replace('"', '""') + '"'


# --------------------------------------------------------------------------- #
#  State file (what was loaded from which CSV, and the last active database)
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            state = json.load(fh)
        if isinstance(state, dict):
            state.setdefault("sources", {})
            return state
    except (OSError, ValueError):
        pass
    return {"active": None, "sources": {}}


def save_state(state: dict) -> None:
    try:
        databases_dir().mkdir(parents=True, exist_ok=True)
        with open(_state_path(), "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
    except OSError:
        pass  # remembering is a convenience, never fatal


def remember_active(name: str) -> None:
    state = load_state()
    if state.get("active") != name:
        state["active"] = name
        save_state(state)


# --------------------------------------------------------------------------- #
#  Introspection
# --------------------------------------------------------------------------- #
@dataclass
class DbInfo:
    name: str
    path: str | None
    is_default: bool            # the practice.duckdb database (hand-made tables)
    folder: Path | None         # data/<folder> it is built from, if any


@dataclass
class SyncReport:
    loaded: list[str] = field(default_factory=list)     # "db.table"
    created: list[str] = field(default_factory=list)    # new databases
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.created:
            parts.append(f"new database(s): {', '.join(self.created)}")
        if self.loaded:
            parts.append(f"loaded {len(self.loaded)} table(s)")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return " · ".join(parts) if parts else "databases up to date"


def current_database(con) -> str:
    return con.execute("select current_database()").fetchone()[0]


def folder_databases() -> dict[str, Path]:
    """Map database name -> data/<folder>, for every folder under data/."""
    root = data_dir()
    if not root.is_dir():
        return {}
    taken = set(_RESERVED)
    out: dict[str, Path] = {}
    for sub in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir() or sub.name.startswith((".", "_")):
            continue
        name = base = ident(sub.name)
        n = 2
        while name.lower() in taken:
            name = f"{base}_{n}"
            n += 1
        taken.add(name.lower())
        out[name] = sub
    return out


def _csv_files(folder: Path) -> list[Path]:
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in _CSV_EXT),
        key=lambda p: p.name.lower(),
    )


def list_databases(con) -> list[DbInfo]:
    """All user databases currently attached, default first then by name."""
    folders = folder_databases()
    dbdir = databases_dir().resolve()
    rows = con.execute(
        "select database_name, path from duckdb_databases() where not internal"
    ).fetchall()
    infos: list[DbInfo] = []
    for name, path in rows:
        in_dbdir = bool(path) and Path(path).resolve().parent == dbdir
        infos.append(DbInfo(name, path, not in_dbdir, folders.get(name)))
    infos.sort(key=lambda d: (not d.is_default, d.name.lower()))
    return infos


def default_database(con) -> str:
    for info in list_databases(con):
        if info.is_default:
            return info.name
    return current_database(con)


def describe_all(con) -> dict[str, list[tuple[str, str, list[tuple[str, str]]]]]:
    """{database: [(schema, table, [(column, type), ...]), ...]} for the browser."""
    result: dict[str, list] = {info.name: [] for info in list_databases(con)}
    rows = con.execute(
        "select table_catalog, table_schema, table_name, column_name, data_type "
        "from information_schema.columns "
        "where table_catalog not in ('system','temp') "
        "and table_schema not in ('information_schema','pg_catalog') "
        "order by table_catalog, table_schema, table_name, ordinal_position"
    ).fetchall()
    index: dict[tuple, list] = {}
    for cat, schema, table, col, dtype in rows:
        key = (cat, schema, table)
        if key not in index:
            index[key] = []
            result.setdefault(cat, []).append((schema, table, index[key]))
        index[key].append((col, dtype))
    # tables with no columns visible (rare) are skipped; empty DBs stay listed
    return result


# --------------------------------------------------------------------------- #
#  Sync
# --------------------------------------------------------------------------- #
def _attached_names(con) -> set[str]:
    return {
        r[0].lower()
        for r in con.execute(
            "select database_name from duckdb_databases() where not internal"
        ).fetchall()
    }


def _signature(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_mtime_ns, st.st_size]


def _load_csv(con, db: str, table: str, path: Path) -> None:
    con.execute(
        f"CREATE OR REPLACE TABLE {qi(db)}.main.{qi(table)} AS "
        "SELECT * FROM read_csv_auto(?, header=true, sample_size=-1)",
        [str(path)],
    )


def sync(con, report: SyncReport | None = None) -> SyncReport:
    """Attach any new folder-databases and load new/changed CSVs. Idempotent."""
    report = report or SyncReport()
    state = load_state()
    sources = state.setdefault("sources", {})
    attached = _attached_names(con)
    databases_dir().mkdir(parents=True, exist_ok=True)

    for name, folder in folder_databases().items():
        db_file = databases_dir() / f"{name}.duckdb"
        if name.lower() not in attached:
            fresh = not db_file.exists()
            try:
                escaped = str(db_file).replace("'", "''")
                con.execute(f"ATTACH '{escaped}' AS {qi(name)}")
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"{name}: cannot attach ({exc})")
                continue
            if fresh:
                sources.pop(name, None)      # file is new: forget old load records
                report.created.append(name)

        known = sources.setdefault(name, {})
        for csv_path in _csv_files(folder):
            table = ident(csv_path.stem)
            sig = _signature(csv_path)
            if known.get(csv_path.name) == sig:
                report.skipped += 1
                continue
            try:
                _load_csv(con, name, table, csv_path)
            except Exception as exc:  # noqa: BLE001
                first = str(exc).splitlines()[0] if str(exc) else repr(exc)
                report.errors.append(f"{name}.{table}: {first}")
                continue
            known[csv_path.name] = sig
            report.loaded.append(f"{name}.{table}")

    save_state(state)
    return report


def setup(con) -> SyncReport:
    """Run on every connect: sync, then restore the last active database."""
    try:
        report = sync(con)
    except Exception as exc:  # noqa: BLE001 - never block startup on this
        report = SyncReport(errors=[f"sync failed: {exc}"])
    wanted = load_state().get("active")
    if wanted and wanted.lower() in _attached_names(con):
        try:
            con.execute(f"USE {qi(wanted)}")
        except Exception:  # noqa: BLE001
            pass
    return report


def use(con, name: str) -> None:
    """Switch the active database (same as `USE name;`) and remember it."""
    known = {info.name.lower(): info.name for info in list_databases(con)}
    real = known.get(name.lower())
    if real is None:
        raise ValueError(
            f"unknown database '{name}'. Available: {', '.join(sorted(known.values()))}")
    con.execute(f"USE {qi(real)}")
    remember_active(real)


def reset(con, name: str) -> SyncReport:
    """Rebuild a folder-backed database from its CSVs, discarding all changes."""
    folders = folder_databases()
    if name not in folders:
        raise ValueError(f"'{name}' is not built from a data/ folder, cannot reset")
    was_active = current_database(con)
    default = default_database(con)
    if was_active == name:
        con.execute(f"USE {qi(default)}")      # DuckDB cannot detach the active DB
    con.execute(f"DETACH {qi(name)}")
    db_file = databases_dir() / f"{name}.duckdb"
    for p in (db_file, Path(str(db_file) + ".wal")):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    state = load_state()
    state.get("sources", {}).pop(name, None)
    save_state(state)
    report = sync(con)
    if was_active.lower() in _attached_names(con):
        con.execute(f"USE {qi(was_active)}")
    return report
