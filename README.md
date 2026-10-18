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

## Three ways to work

### 1. Native IDE (PyQt5)  ← the comfortable one

Double-click **`ide.bat`** (or run `sqlpad ide`). A small native window: write
SQL in the top editor with syntax highlighting, press **Ctrl+Enter** to run.

- **Ctrl+Enter** runs the selection, or the statement under the cursor.
- **Ctrl+Shift+Enter** runs the whole editor.
- The left panel is a **database browser**: database → tables → columns.
  Double-click a database to make it the active one (same as `USE name;`),
  double-click a table to preview 100 rows. Right-click for more (reset a
  database from its CSVs, open its folder, count rows).
- Toolbar: **Sync** (load new/changed CSVs), dialect switcher, Open/Save `.sql`,
  Export results to CSV. The active database is shown next to the buttons.

See [Databases](#databases-one-folder--one-database) for how the left panel is filled.

### 2. Live `.sql` file watcher

Double-click **`watch.bat`** (or run `sqlpad watch`). It watches
[`query.sql`](query.sql). Open that file in any editor, write SQL, hit **save**,
and the watcher re-runs the whole file and prints every result.

No quotes. No backslashes. No wrapping it in a function. Just SQL in a file.

```
edit query.sql  ──save──►  watcher re-runs  ──►  formatted tables appear
```

### 3. Interactive REPL

Double-click **`repl.bat`** (or run `sqlpad repl`). A prompt with command
history, SQL syntax highlighting, and multi-line editing (like the mysql
client): type across lines, press **Enter** to run once the statement ends
with `;`.

```
[shop] sql> SELECT city, COUNT(*)
          > FROM customers
          > GROUP BY city;
```

The prompt shows the active database. `.databases`, `.use <db>`, `.sync`.

---

## Databases (one folder = one database)

Every **subfolder of `data/`** is a database, and every **CSV in it is a table**
named after the file. Add a database by creating a folder and dragging CSVs in:

```
data/
  sql_cookbook_csv/   ← database "sql_cookbook_csv"  (emp.csv → table emp, ...)
  shop/               ← database "shop"              (customers, orders)
  my_new_db/          ← drop CSVs here → new database on the next start / Sync
```

- **Pick the active database** by double-clicking it in the IDE, or with
  `USE sql_cookbook_csv;` (any interface). Unqualified names (`SELECT * FROM emp`)
  resolve **only in the active database**, like MSSQL. Cross-database works with
  `db.table`: `SELECT * FROM shop.customers`. The last active one is remembered.
- **Names:** the database name is the folder name; the table name is the file
  name. Characters other than letters, digits and `_` become `_`
  (`My Data-1` → `My_Data_1`), so rename the folder if you want a shorter name.
- **`practice`** is the default database (`practice.duckdb`): the home of tables
  you create by hand with `CREATE TABLE`.
- **Sync:** on every start, new or *changed* CSVs are loaded; unchanged ones are
  not touched. Press **⟳ Sync** (or `.sync`, `sqlpad sync`) to pick up files you
  dropped while the app was open.
- **Your changes are kept.** `UPDATE`/`DELETE`/`DROP` and tables you create stay
  across sessions (a CSV is only reloaded when the CSV file itself changes).
  To get a pristine copy back: right-click the database → **Reset from CSV
  files…** (or `.reset <db>`, `sqlpad reset <db>`). Reset discards everything
  you changed in that database.
- The generated DuckDB files live in `databases/` (git-ignored). The CSVs in
  `data/` are the source of truth.
- Column types are guessed by DuckDB. A column that is empty in every row (e.g.
  `emp_commission.comm`) is guessed as text; fix it with
  `ALTER TABLE emp_commission ALTER comm TYPE INTEGER;` (Reset undoes it).

```bash
sqlpad databases            # list databases and their table counts
sqlpad sync                 # load new/changed CSVs now
sqlpad reset <database>     # rebuild one database from its CSVs
```

---

## Getting data in

> ⚠️ **A table only exists once it is loaded.** Files in a `data/<folder>/` are
> loaded automatically (see [Databases](#databases-one-folder--one-database)).
> Any other file, wherever it lives, must be *imported* first.

**Easiest: drag a CSV into a `data/<folder>/`** and press ⟳ Sync.

**Or import into the active database** — DuckDB sniffs delimiter, header and
column types for you:

```bash
sqlpad import path/to/customers.csv customers
```
…or inside the REPL: `.import path/to/customers.csv customers`
…or inside SQL directly:
```sql
CREATE OR REPLACE TABLE customers AS
    SELECT * FROM read_csv_auto('path/to/customers.csv', header = true);
```

**From Excel (.xlsx)** — same command; it uses pandas under the hood (offline):

```bash
sqlpad import path/to/book.xlsx emp              # first sheet
sqlpad import path/to/book.xlsx emp --sheet 1    # 2nd sheet, or --sheet "Sheet2"
```
…or inside the REPL: `.import path/to/book.xlsx emp [sheet]`

**Made-up data by hand** — just write normal DDL/DML; it persists:

```sql
CREATE OR REPLACE TABLE fruit (name VARCHAR, qty INTEGER);
INSERT INTO fruit VALUES ('apple', 5), ('pear', 2);
SELECT * FROM fruit ORDER BY qty DESC;
```

Two ready-made sample tables ship in the `shop` database ([`data/shop/`](data/shop/):
`customers`, `orders`) so you can practice JOINs and window functions on day one —
see [`query.sql`](query.sql).

**SQL Cookbook** (Anthony Molinaro) — all of the book's tables (`emp`, `dept`,
`emp_bonus`, `t1`…`t500`, …) are the CSVs in
[`data/sql_cookbook_csv/`](data/sql_cookbook_csv/), i.e. the database
`sql_cookbook_csv`. `USE sql_cookbook_csv;` and follow the book.

---

## Command reference

```bash
sqlpad ide                  # native PyQt5 IDE (editor + results grid)
sqlpad watch [file.sql]     # live file runner (default; file defaults to query.sql)
sqlpad repl                 # interactive prompt
sqlpad import <file> <table> [--sheet N]  # load a CSV or Excel (.xlsx) file
sqlpad run "<sql>"          # run one query, print, exit
sqlpad tables               # list tables in the active database
sqlpad databases            # list databases (folders under data/)
sqlpad sync                 # load new/changed CSVs from data/
sqlpad reset <database>     # rebuild a database from its CSVs (discards changes)
```

Global options (before the subcommand): `-d/--dialect <standard|mysql|bigquery>`
and `--db <file>` (or set `$SQLPAD_DB`).

### REPL meta-commands
```
.help                 show help
.databases            list databases
.use <db>             switch the active database (same as: USE <db>;)
.sync                 load new/changed CSVs from data/ now
.reset <db>           rebuild a database from its CSVs (discards changes)
.tables               list tables in the active database
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
