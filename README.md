# SQLPad

A tiny local SQL-practice environment. **No installs, no servers, no
SQL-inside-Python-strings.** You write plain SQL — multi-line, unescaped,
exactly like you would in a real client — and see formatted results instantly.

Powered by **DuckDB** (already on your machine via Anaconda). Tables persist in
a single file (`practice.duckdb`) between sessions.

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

**Made-up data by hand** — just write normal DDL/DML; it persists:

```sql
CREATE OR REPLACE TABLE fruit (name VARCHAR, qty INTEGER);
INSERT INTO fruit VALUES ('apple', 5), ('pear', 2);
SELECT * FROM fruit ORDER BY qty DESC;
```

Two ready-made sample tables ship in [`data/`](data/) (`customers`, `orders`)
so you can practice JOINs and window functions on day one — see
[`query.sql`](query.sql).

---

## Command reference

```bash
sqlpad watch [file.sql]     # live file runner (default; file defaults to query.sql)
sqlpad repl                 # interactive prompt
sqlpad import <csv> <table> # load a CSV into a table
sqlpad run "<sql>"          # run one query, print, exit
sqlpad tables               # list tables
```

Options: `--db <file>` (or set `SQLPAD_DB`) to use a different database file.

### REPL meta-commands
```
.help                 show help
.tables               list tables
.schema [table]       show columns
.import <csv> <table> load a CSV
.read <file.sql>      run a .sql file
.width <n>            max column width (default 40)
.quit                 leave
```

---

## MySQL vs DuckDB — the small print

DuckDB speaks standard analytical SQL, so the vast majority of MySQL practice
works unchanged: `JOIN`, `GROUP BY`, `HAVING`, subqueries, CTEs (`WITH`),
`CASE`, window functions (`ROW_NUMBER`, `RANK`, `SUM() OVER …`), etc.

A few MySQL-isms differ:

| MySQL                       | DuckDB equivalent                    |
|-----------------------------|--------------------------------------|
| `IFNULL(x, y)`              | `COALESCE(x, y)` (also in MySQL)     |
| `` `col` `` (backtick quote)| `"col"` (double quotes)              |
| `LIMIT n, m`                | `LIMIT m OFFSET n`                   |
| `DATE_FORMAT(d, '%Y-%m')`   | `strftime(d, '%Y-%m')`               |
| `GROUP_CONCAT(x)`           | `string_agg(x, ',')`                 |
| `x DIV y`                   | `x // y`                             |

For practising query *logic* — the actual skill — it's an excellent stand-in.

---

## How it works (no magic)

- `sqlpad/core.py` — DuckDB connection, CSV import, statement execution.
- `sqlpad/render.py` — splits scripts into statements and draws the box tables.
- `sqlpad/watch.py` — polls `query.sql`'s modified-time and re-runs on change.
- `sqlpad/repl.py` — prompt_toolkit prompt + meta-commands.

Everything is plain Python stdlib + `duckdb` + `prompt_toolkit`, all already
installed. Nothing talks to the network. Nothing needs admin rights.
