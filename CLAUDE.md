# CLAUDE.md — SQLPad project notes

Context for continuing work on this project. Read this first.

## What this is

**SQLPad** (GitHub repo `EladOzCohen/sql__emulator`) is a local SQL-practice
tool for a data analyst who, for security reasons, cannot install SQL programs
on the work PC and dislikes querying through the DuckDB Python API (writing SQL
inside Python strings). SQLPad lets the user write plain `.sql` and see
formatted results, with no servers and no installs.

- **Engine:** DuckDB, persisted to `practice.duckdb` (gitignored).
- **Two interfaces:** a live `.sql` file watcher, and a prompt_toolkit REPL.
- **Dialects:** practice `standard` (DuckDB), `mysql`, or `bigquery` (GoogleSQL).
- It is honest **local syntax practice**, NOT a real MySQL/BigQuery server.

## Environment & constraints (important)

- Python is **Anaconda, NOT on PATH**: `C:\Users\eladoc\AppData\Local\anaconda3\python.exe`
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
  `C:\Users\eladoc`. **Always commit to the sqlpad repo, never the home one.**
- The user controls pushes — commit when asked, do not `git push` unless told.
- Last commits:
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
#   tables                list tables
```

`--db` or `$SQLPAD_DB` overrides the database file. `-d` aliases: `bq`, `duckdb`,
`my`, `google`.

## Architecture / file map

```
sqlpad/
  __main__.py   CLI dispatch; --dialect flag + interactive entry menu
  core.py       connect(db, prelude), run_statement -> Result, import_file (CSV+Excel), list_tables
  dialects.py   Dialect dataclass + registry (standard/mysql/bigquery), resolve()
  render.py     iter_statements()/split_statements()/statement_at() (quote/comment-aware),
                render_table() box output
  watch.py      poll scratch file mtime (0.4s), re-run whole file, print results
  repl.py       prompt_toolkit prompt, Enter submits on ';' , meta-commands
  ide.py        PyQt5 IDE: editor + SqlHighlighter, results grid, table list,
                dialect combo, open/save .sql, CSV export; Ctrl+Enter = run
                stmt-at-cursor/selection, Ctrl+Shift+Enter = run all
query.sql           standard scratch file
query_mysql.sql     mysql scratch file
query_bigquery.sql  bigquery scratch file
data/customers.csv, data/orders.csv   sample tables for JOIN/window practice
data/emp.xlsx                         SQL Cookbook EMP table (user-added; .import it)
data/seed_sqlcookbook.sql             SQL Cookbook helper tables DEPT/T1/T10/T100/T500
*.bat               Windows launchers
```

REPL meta-commands: `.help .tables .schema .import .read .dialect .width .quit`.
`.dialect [name]` shows the cheat-sheet or switches dialect on the fly
(reconnects to clear the previous dialect's TEMP macros).

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

- **A file in `data/` is not a table** — it must be `.import`ed first, or
  `SELECT` won't find it. Common user confusion.
- Import dispatches on extension (`core.import_file`): CSV/TSV → DuckDB
  `read_csv_auto`; Excel `.xlsx/.xlsm/.xls/.xlsb` → pandas `read_excel` (lazy
  import, offline, needs openpyxl which is installed) registered into DuckDB.
  `--sheet` / 3rd `.import` arg picks the sheet (index or name).
- Aggregate functions can't be macros (so COUNTIF stays `count_if`, etc.).
- `UNNEST(list) AS n` binds a struct in DuckDB; use `AS t(n)`.
- MySQL `DATE_FORMAT` minute token is `%i`; DuckDB `strftime` uses `%M`.
- Git warns `LF will be replaced by CRLF` on commit — harmless (Windows autocrlf).

## Status

Done & tested (all three scratch files run green; both dialects work via `run`;
dialect switch and unknown-dialect error handled). Nothing pending or broken.

## Possible next steps (backlog, none committed to)

- `EXPORT`/`.export <table|query> <csv>` to dump a result to CSV.
- More BigQuery function macros where feasible (e.g. SAFE_DIVIDE variants done;
  consider FORMAT, GENERATE_DATE_ARRAY).
- Optional opt-in text rewriter for the parser-level BigQuery differences
  (DATE_TRUNC keyword args, `* EXCEPT`). Deliberately NOT done — silent rewrites
  are hard to debug and can teach wrong habits. Only add if the user asks.
- An "m/-mysql compatibility shim" was floated earlier and also deferred.

## Working with this user

See personal memory for standing preferences (Hebrew replies, ask-don't-guess,
step-by-step, Netskope work PC). Replies to the user are in Hebrew.
