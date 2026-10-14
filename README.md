# SQLPad

A tiny local SQL-practice environment. **No installs, no servers, no
SQL-inside-Python-strings.** You write plain SQL — multi-line, unescaped,
exactly like you would in a real client — and see formatted results instantly.

Powered by **DuckDB** (already on your machine via Anaconda). Tables persist in
a single file (`practice.duckdb`) between sessions.

Practice in **three dialects** — Standard/DuckDB, **MySQL**, or **BigQuery
(GoogleSQL)** — see [Dialects](#dialects) below.

---

## Dialects

SQLPad always runs on DuckDB, but you can practice in a chosen dialect. Picking
one loads a small compatibility layer so dialect-specific syntax runs unchanged,
and gives you a cheat-sheet of the real differences.

Pick a dialect three ways:

- **Entry menu** — double-click `watch.bat` or `repl.bat`; it asks which dialect.
- **Flag** — `sqlpad -d bigquery watch` / `-d mysql` / `-d standard` (aliases: `bq`, `duckdb`).
- **BigQuery shortcut** — double-click `bigquery.bat` to jump straight into GoogleSQL mode.

Each dialect has its **own scratch file** so your work stays separate:

| Dialect              | Scratch file          | REPL prompt |
|----------------------|-----------------------|-------------|
| Standard / DuckDB    | `query.sql`           | `sql>`      |
| MySQL-flavored       | `query_mysql.sql`     | `mysql>`    |
| BigQuery (GoogleSQL) | `query_bigquery.sql`  | `bq>`       |

In the REPL, `.dialect` shows the current cheat-sheet and `.dialect bigquery`
switches on the fly. The compatibility macros are session-only (`TEMP`), so
switching never pollutes your saved tables or the other dialect.

> **Honest scope:** this is local *syntax practice*, not a real MySQL/BigQuery
> server (those can't run without installing/connecting to them). ~90% of what
> you'd practice runs unchanged; the handful of parser-level differences are
> listed per dialect below so you learn the exact translation.

---

## Two ways to work

### 1. Live `.sql` file watcher  ← the fix for your debugging pain

Double-click **`watch.bat`** (or run `sqlpad watch`). It watches
[`query.sql`](query.sql). Open that file in any editor, write SQL, hit **save**,
and the watcher re-runs the whole file and prints every result.

No quotes. No backslashes. No wrapping it in a function. Just SQL in a file.

```
edit query.sql  ──save──►  watcher re-runs  ──►  formatted tables appear
```

### 2. Interactive REPL

Double-click **`repl.bat`** (or run `sqlpad repl`). A prompt with command
history, SQL syntax highlighting, and multi-line editing (like the mysql
client): type across lines, press **Enter** to run once the statement ends
with `;`.

```
sql> SELECT city, COUNT(*)
   > FROM customers
   > GROUP BY city;
```

---

## Getting data in

> ⚠️ **Dropping a file into `data/` does NOT create a table.** A table exists
> only after you *import* it. Until then `SELECT * FROM emp` will say the table
> isn't found.

**From a CSV** — DuckDB sniffs delimiter, header and column types for you:

```bash
sqlpad import data/customers.csv customers
```
…or inside the REPL: `.import data/customers.csv customers`
…or inside SQL directly:
```sql
CREATE OR REPLACE TABLE customers AS
    SELECT * FROM read_csv_auto('data/customers.csv', header = true);
```

**From Excel (.xlsx)** — same command; it uses pandas under the hood (offline):

```bash
sqlpad import data/emp.xlsx emp              # first sheet
sqlpad import data/emp.xlsx emp --sheet 1    # 2nd sheet, or --sheet "Sheet2"
```
…or inside the REPL: `.import data/emp.xlsx emp [sheet]`

**Made-up data by hand** — just write normal DDL/DML; it persists:

```sql
CREATE OR REPLACE TABLE fruit (name VARCHAR, qty INTEGER);
INSERT INTO fruit VALUES ('apple', 5), ('pear', 2);
SELECT * FROM fruit ORDER BY qty DESC;
```

Two ready-made sample tables ship in [`data/`](data/) (`customers`, `orders`)
so you can practice JOINs and window functions on day one — see
[`query.sql`](query.sql).

**SQL Cookbook tables** — to follow Anthony Molinaro's *SQL Cookbook*, load its
helper tables (`DEPT`, `T1`, `T10`, `T100`, `T500`) any time with:

```
.read data/seed_sqlcookbook.sql
```

(`EMP` is the book's employee table — import it from `data/emp.xlsx`:
`.import data/emp.xlsx emp`. `EMP.DEPTNO` joins to `DEPT.DEPTNO`.)

---

## Command reference

```bash
sqlpad watch [file.sql]     # live file runner (default; file defaults to query.sql)
sqlpad repl                 # interactive prompt
sqlpad import <file> <table> [--sheet N]  # load a CSV or Excel (.xlsx) file
sqlpad run "<sql>"          # run one query, print, exit
sqlpad tables               # list tables
```

Global options (before the subcommand): `-d/--dialect <standard|mysql|bigquery>`
and `--db <file>` (or set `$SQLPAD_DB`).

### REPL meta-commands
```
.help                 show help
.tables               list tables
.schema [table]       show columns
.import <file> <table> [sheet]   load a CSV or Excel (.xlsx) file
.read <file.sql>      run a .sql file
.dialect [name]       show cheat-sheet, or switch dialect on the fly
.width <n>            max column width (default 40)
.quit                 leave
```

---

## BigQuery (GoogleSQL) — the small print

Runs native (write it exactly as in BigQuery):
`QUALIFY`, `GROUP BY <alias>`, `GROUP BY ALL`, trailing commas, `COUNT_IF`,
`APPROX_COUNT_DISTINCT`, `LOGICAL_AND/OR`, `STARTS_WITH`, `ENDS_WITH`,
`REGEXP_EXTRACT`, `STRING_AGG`, `ARRAY_AGG`, `UNNEST`.

Provided by the BigQuery prelude (macros, so they just work):
`SAFE_DIVIDE` / `SAFE_ADD` / `SAFE_SUBTRACT` / `SAFE_MULTIPLY` / `SAFE_NEGATE`,
`FORMAT_DATE` / `PARSE_DATE` / `FORMAT_TIMESTAMP` / `PARSE_TIMESTAMP`,
`REGEXP_CONTAINS`, `SPLIT`, `GENERATE_ARRAY`.

Parser-level differences no macro can fix — translate these by hand:

| BigQuery (GoogleSQL)        | DuckDB equivalent                     |
|-----------------------------|---------------------------------------|
| `SELECT * EXCEPT(col)`      | `SELECT * EXCLUDE (col)`              |
| `SAFE_CAST(x AS INT64)`     | `TRY_CAST(x AS BIGINT)`               |
| `DATE_TRUNC(d, MONTH)`      | `date_trunc('month', d)`              |
| `DATE_DIFF(d1, d2, DAY)`    | `date_diff('day', d2, d1)` (order!)   |
| `STRUCT(1 AS a, 2 AS b)`    | `{'a': 1, 'b': 2}`                    |
| `UNNEST(arr) AS x`          | `UNNEST(arr) AS t(x)`                 |
| `COUNTIF(cond)`             | `count_if(cond)` (underscore)         |
| `SAFE.FN(...)`              | plain `FN(...)` (no SAFE. prefix)     |
| `INT64 / FLOAT64 / STRING`  | `BIGINT / DOUBLE / VARCHAR`           |

## MySQL — the small print

Provided by the MySQL prelude: `DATE_FORMAT`, `STR_TO_DATE`, `IFNULL`.

| MySQL                       | DuckDB equivalent                     |
|-----------------------------|---------------------------------------|
| `` `col` `` (backtick quote)| `"col"` (double quotes)               |
| `LIMIT n, m`                | `LIMIT m OFFSET n`                    |
| `GROUP_CONCAT(x)`           | `string_agg(x, ',')`                  |
| `x DIV y`                   | `x // y`                              |
| `DATE_FORMAT` minute `%i`   | `%M` here (DuckDB strftime)           |

For practising query *logic* — the actual skill — all three dialects are
excellent stand-ins.

---

## How it works (no magic)

- `sqlpad/core.py` — DuckDB connection, CSV import, statement execution.
- `sqlpad/dialects.py` — dialect profiles: compatibility macros + cheat-sheets.
- `sqlpad/render.py` — splits scripts into statements and draws the box tables.
- `sqlpad/watch.py` — polls the scratch file's modified-time and re-runs on change.
- `sqlpad/repl.py` — prompt_toolkit prompt + meta-commands.

Everything is plain Python stdlib + `duckdb` + `prompt_toolkit`, all already
installed. Nothing talks to the network. Nothing needs admin rights.
