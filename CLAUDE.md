# CLAUDE.md — SQLPad project notes

Context for continuing work on this project. Read this first.

## What this is

**SQLPad** (GitHub repo `EladOzCohen/sql__emulator`) is a local SQL-practice
tool for a data analyst who, for security reasons, cannot install SQL programs
on the work PC and dislikes querying through the DuckDB Python API (writing SQL
inside Python strings). SQLPad lets the user write plain `.sql` and see
formatted results, with no servers and no installs.

- **Engine:** DuckDB. The default database persists to `practice.duckdb`
  (gitignored); folder-backed databases live in `databases/` (gitignored).
- **Three interfaces:** a native PyQt5 IDE, a live `.sql` file watcher, and a
  prompt_toolkit REPL.
- **Databases:** every subfolder of `data/` is a database, every CSV in it a
  table (MSSQL-style; see "Databases (catalog)" below). Current use: the user is
  practising the O'Reilly *SQL Cookbook* (database `sql_cookbook_csv`).
- **Dialects:** practice `standard` (DuckDB), `mysql`, or `bigquery` (GoogleSQL).
- It is honest **local syntax practice**, NOT a real MySQL/BigQuery server.

## Environment & constraints (important)

- Python is **Anaconda, NOT on PATH**: `%USERPROFILE%\AppData\Local\anaconda3\python.exe`
  (Python 3.13). `python`/`py` fail in the shell — always use the full path.
- Preinstalled and relied on: `duckdb` 1.5.5, `prompt_toolkit` 3.0.52, `pygments`,
  `pandas` (+`openpyxl` for Excel import), `PyQt5` (the IDE), stdlib `sqlite3`.
  **Do not add dependencies** that aren't already in Anaconda — the whole point
  is zero installs. No network either.
- Platform: Windows 11. Shell for the user is PowerShell; `.bat` launchers wrap
  the Anaconda python path defensively.

## Git

- This folder has its **own repo**: branch `main`, remote `origin` =
  https://github.com/EladOzCohen/sql__emulator.git.
- ⚠️ This folder also sits inside a messy home-dir git repo rooted at
  the user home directory. **Always commit to the sqlpad repo, never the home one.**
- The user controls pushes — commit when asked, do not `git push` unless told.
- Last commits on `main` (the databases feature below is NOT committed yet unless
  `git log` shows a newer commit):
  - `8584db7` Add native PyQt5 SQL IDE
  - `0a611cd` Add Excel import, SQL Cookbook tables, and project docs
  - `0c84d01` Add MySQL and BigQuery dialect practice modes
  - `d8e3956` first version, only duckDB sql syntax support

## How to run

```bash
# via launchers (double-click on Windows):
ide.bat          # native PyQt5 IDE (uses pythonw; optional arg: a dialect)
watch.bat        # menu -> live .sql watcher
repl.bat         # menu -> interactive REPL
bigquery.bat     # watcher straight in BigQuery mode

# via CLI (use the full python path on this machine):
python -m sqlpad [-d standard|mysql|bigquery] [--db FILE] <command>
#   ide                   native PyQt5 IDE (editor + results grid)
#   watch [file.sql]      live file runner (default; file defaults per dialect)
#   repl                  interactive prompt
#   import <file> <table> [--sheet N]   load a CSV or Excel (.xlsx) file
#   run "<sql>"           run one query, print, exit
#   tables                list tables in the active database
#   databases             list databases + table counts
#   sync                  load new/changed CSVs from data/
#   reset <db> [-y]       rebuild a database from its CSVs (discards changes)
```

`--db` or `$SQLPAD_DB` overrides the default database file. `$SQLPAD_DATA`
overrides the `data/` folder (the generated `databases/` folder is its sibling) —
used by tests to work in a temp workspace. `-d` aliases: `bq`, `duckdb`, `my`,
`google`.

## Architecture / file map

```
sqlpad/
  __main__.py   CLI dispatch; --dialect flag + interactive entry menu
  core.py       connect()/connect_ex() (open default db + catalog.setup + dialect prelude),
                run_statement -> Result (hides USE's bare "Success" column),
                import_file (CSV+Excel into the ACTIVE db), list_tables
  catalog.py    databases: folder->database mapping, ATTACH, CSV sync, reset,
                active-db memory, describe_all() for the IDE tree
  dialects.py   Dialect dataclass + registry (standard/mysql/bigquery), resolve()
  render.py     iter_statements()/split_statements()/statement_at() (quote/comment-aware),
                render_table() box output
  watch.py      poll scratch file mtime (0.4s), re-run whole file, print results
  repl.py       prompt_toolkit prompt, Enter submits on ';' , meta-commands
  ide.py        PyQt5 IDE: editor + SqlHighlighter, results grid, database tree
                (db -> tables -> columns; context menu), dialect combo, Sync,
                open/save .sql, CSV export; Ctrl+Enter = run stmt-at-cursor/
                selection, Ctrl+Shift+Enter = run all. Dark QSS incl. scrollbars.
query.sql           standard scratch file   (all three start with `USE shop;`)
query_mysql.sql     mysql scratch file
query_bigquery.sql  bigquery scratch file
data/<folder>/*.csv     each folder = a database (source of truth, committed)
  data/shop/              customers.csv, orders.csv (JOIN/window practice)
  data/sql_cookbook_csv/  the 21 tables of the SQL Cookbook (emp, dept, t1..t500, ...)
databases/              GENERATED: one <db>.duckdb per folder + _state.json (gitignored)
*.bat               Windows launchers
```

REPL meta-commands: `.help .databases .use .sync .reset .tables .schema .import
.read .dialect .width .quit`. The prompt shows the active database: `[shop] sql> `.
`.dialect [name]` shows the cheat-sheet or switches dialect on the fly
(reconnects to clear the previous dialect's TEMP macros; the active database is
restored from `_state.json`).

## Databases (catalog) — design and rules

User request: group tables into "databases" like MSSQL, selectable on the left
of the IDE; adding a database = making a subfolder of `data/` and dragging CSVs in.

- **Folder = database, CSV = table.** Names via `catalog.ident()`: non-`[A-Za-z0-9_]`
  chars become `_`, leading digit gets `_`. Reserved/colliding names get `_2`.
  Folders starting with `.` or `_` are ignored. Only `.csv`/`.tsv` are loaded
  (Excel inside folders is not supported yet).
- **One DuckDB file per database** (`databases/<name>.duckdb`), ATTACHed on every
  connect. Chosen over schemas-in-one-file for real isolation, per-database reset
  and no name collisions (the cookbook's `emp/dept/t1..` collide with old tables).
  The default database is named after `practice.duckdb`'s stem: `practice`.
- **Strict like MSSQL:** unqualified names resolve only in the active database
  (`USE name;` or tree double-click). `db.table` works across databases. No
  fallback search across databases (user confirmed this model).
- **Active database is remembered** in `databases/_state.json` (`active`), updated
  by the IDE/REPL after every statement (so a typed `USE x;` counts). The watcher
  does not persist it (scratch files start with an explicit `USE`).
- **Sync rule:** a CSV is (re)loaded only if new or its (mtime_ns, size) differs
  from `_state.json -> sources[db][file]`. Table existence is NOT checked, so a
  table the user dropped is not resurrected and DML changes survive restarts.
  If a database file is newly created, its old state entries are discarded.
  Per-file load errors go into `SyncReport.errors`; they never block startup.
- **Reset** (`catalog.reset`): switch to the default db (DuckDB cannot DETACH the
  active database), DETACH, delete the `.duckdb`/`.wal`, clear its state, re-sync,
  restore the active db. Destroys user changes -> IDE asks for confirmation,
  CLI/REPL ask the user to type the database name (`-y` skips in the CLI).
- User decisions: changes persist + Reset button (not "always pristine");
  existing customers/orders moved to `data/shop/`.

## Testing without touching the user's real data

The IDE/CLI hold DuckDB file locks, so never test against the real
`practice.duckdb` while the user may have the IDE open. Use a temp workspace:
set `$SQLPAD_DATA` to a temp `data/` dir (copy CSVs in) and a temp `--db`/db path.
GUI check without showing a window: `win.setAttribute(Qt.WA_DontShowOnScreen)`,
`win.show()`, then `win.grab().save("x.png")` and Read the PNG.

## Dialect system (the key design)

Everything runs on DuckDB. A `Dialect` (in `dialects.py`) carries: `key`,
`title`, REPL `prompt`, default `scratch` file, a `prelude` (list of statements),
and a `cheatsheet`.

- The prelude is a set of **`CREATE OR REPLACE TEMP MACRO`** statements that
  emulate dialect functions on DuckDB. `TEMP` = session-only, so they vanish on
  disconnect and never pollute `practice.duckdb` or another dialect.
- `core.connect(db, prelude=dial.prelude)` runs the prelude right after connect.

Three tiers of BigQuery/MySQL support:
1. **Native** (write as-is): QUALIFY, GROUP BY alias, GROUP BY ALL, trailing
   commas, COUNT_IF, APPROX_COUNT_DISTINCT, STARTS_WITH/ENDS_WITH,
   REGEXP_EXTRACT, STRING_AGG, ARRAY_AGG, UNNEST, IFNULL.
2. **Emulated via TEMP macros:** SAFE_DIVIDE/ADD/SUBTRACT/MULTIPLY/NEGATE,
   FORMAT_DATE/PARSE_DATE/FORMAT_TIMESTAMP/PARSE_TIMESTAMP, REGEXP_CONTAINS,
   SPLIT, GENERATE_ARRAY (BigQuery); DATE_FORMAT, STR_TO_DATE (MySQL).
3. **Cannot emulate (parser-level) — documented, not rewritten:**
   `* EXCEPT(col)`→`EXCLUDE`, `SAFE_CAST(x AS INT64)`→`TRY_CAST(x AS BIGINT)`,
   `DATE_TRUNC(d, MONTH)`→`date_trunc('month', d)`,
   `DATE_DIFF(d1,d2,DAY)`→`date_diff('day', d2, d1)` (note order),
   `STRUCT(1 AS a)`→`{'a':1}`, `UNNEST(arr) AS x`→`UNNEST(arr) AS t(x)`,
   `COUNTIF`→`count_if`, `SAFE.FN()`→plain FN, INT64/FLOAT64/STRING→BIGINT/DOUBLE/VARCHAR.

**To add/extend a dialect:** edit `dialects.py` — add macros to the `prelude`
and lines to the `cheatsheet`. DuckDB macros are scalar/table only (NO aggregate
macros), so aggregate-style functions must already exist natively or be
documented. Verify each new macro empirically against DuckDB 1.5.5 before
shipping (several BigQuery functions take bare keyword args that won't parse).

## Gotchas learned

- **A loose file in `data/` is not a table** — only files inside a
  `data/<folder>/` are auto-loaded; anything else must be `.import`ed (into the
  active database). Common user confusion.
- **Fresh clone / second PC:** `practice.duckdb` and `databases/` are gitignored
  but the CSVs under `data/<folder>/` are committed, so the first start rebuilds
  every database via `catalog.setup()`. This replaced the short-lived
  `core.load_sample_data()` (commit `2a6cab3`, which imported loose `data/` files
  and `seed_*.sql` into the default db); that mechanism and its seed files are gone.
- Running on a second PC (user `elado`, Anaconda at `%USERPROFILE%\anaconda3`):
  `duckdb` was not preinstalled there and had to be `pip install`ed.
- DuckDB locks the db file: while the IDE is open, CLI commands on the same
  `practice.duckdb` (or any attached `databases/*.duckdb`) fail with "being used
  by another process".
- DuckDB infers an all-NULL column from CSV as VARCHAR (e.g. `emp_commission.comm`).
  No per-column type override exists yet (see backlog).
- `USE x` returns a result with one `Success` column and no rows; `run_statement`
  treats that as "no result set".
- Import dispatches on extension (`core.import_file`): CSV/TSV → DuckDB
  `read_csv_auto`; Excel `.xlsx/.xlsm/.xls/.xlsb` → pandas `read_excel` (lazy
  import, offline, needs openpyxl which is installed) registered into DuckDB.
  `--sheet` / 3rd `.import` arg picks the sheet (index or name).
- Aggregate functions can't be macros (so COUNTIF stays `count_if`, etc.).
- `UNNEST(list) AS n` binds a struct in DuckDB; use `AS t(n)`.
- MySQL `DATE_FORMAT` minute token is `%i`; DuckDB `strftime` uses `%M`.
- Git warns `LF will be replaced by CRLF` on commit — harmless (Windows autocrlf).

## Status

Done & tested: dialects, Excel import, IDE (committed), and the databases feature
(catalog sync/attach/use/reset, IDE tree, REPL/CLI commands; verified on a temp
workspace and on the real data: 21 cookbook tables + 2 shop tables load).
Cleanup done with the user's approval: removed the redundant `data/emp.xlsx` and
`data/seed_sqlcookbook.sql` (the cookbook CSVs replace them) and dropped the 8
old duplicate tables from `practice.duckdb`, which is now an empty sandbox.
`sql_cookbook_tables.zip` (untracked, repo root) is the user's original download
of the cookbook CSVs; deliberately left alone and not committed.

## Possible next steps (backlog, none committed to)

- Per-folder type overrides (e.g. `data/<folder>/_types.json`) for all-NULL
  columns that DuckDB guesses as VARCHAR.
- Load `.xlsx` files placed inside a `data/<folder>/` (sheet per table?).
- CLI `--use <db>` global option; watcher header showing the active database.
- An `.export <query> <csv>` command in the REPL (the IDE already has Export CSV).
- More BigQuery function macros where feasible (e.g. SAFE_DIVIDE variants done;
  consider FORMAT, GENERATE_DATE_ARRAY).
- Optional opt-in text rewriter for the parser-level BigQuery differences
  (DATE_TRUNC keyword args, `* EXCEPT`). Deliberately NOT done — silent rewrites
  are hard to debug and can teach wrong habits. Only add if the user asks.
- An "m/-mysql compatibility shim" was floated earlier and also deferred.

## Working with this user

See personal memory for standing preferences (Hebrew replies, ask-don't-guess,
step-by-step). Replies to the user are in Hebrew.

## Dialect fidelity work (in progress)

Goal: SQL written in MySQL / BigQuery syntax must run and return what the real
engine returns. Architecture: `sqlpad/translate.py` (tokenizer + call handlers +
rules; unaliased columns keep MySQL's expression text or BigQuery's `f0_`),
`sqlpad/mysql/` and `sqlpad/bigquery/` packages (one module per area: temporal,
strings, numeric/mathconv, control, aggregate, arrays, admin). Dialect objects
now carry `session` settings, `translate`, `register` (Python UDFs) and `prelude`.
`core.run_statement(con, sql, dialect)` translates first; errors show the
translated SQL ("ran as (DuckDB)"); IDE button "⇄ SQL" shows it without running.

QA: `python tests/compat/run_compat.py --dialect mysql|bigquery [--file X] [-v]`
runs examples taken from the official manuals (`tests/compat/cases/*.json`)
and `tests/compat/audit_functions.py`
lists documented functions that do not exist. Principled skips live in
`tests/compat/known_skips*.json` (each needs a reason).

Status (MySQL): all 518 manual examples pass or are skipped with a written reason (477 pass, 41 documented gaps in known_skips*.json; run tests/compat/regressions.py too). Known MySQL gaps: implicit text->number arithmetic ('1'+1), CAST AS UNSIGNED/YEAR, mixed-type IN/BETWEEN/IFNULL, _binary. BigQuery: 619 of 786 manual examples pass, 158 skipped with reasons (many only differ in how bytes/structs/DATETIME are displayed), 9 still fail (UNNEST as a column alias, FORMAT('%T'), octal escapes in b'..', COLLATE). Never define macros named like operators or core aggregates/window functions (they break SUM(x)+1 and windows); tests/compat/regressions.py guards this.
Gotchas: a TEMP macro named like a builtin shadows it; inside macros call the
real one as `system.main.<fn>()`, and never rely on DuckDB builtins that call
another shadowed builtin (`round_even` calls `round`). Run suites with a
timeout: a recursive macro hangs DuckDB instead of failing.
