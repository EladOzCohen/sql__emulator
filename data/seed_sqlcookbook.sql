-- ┌──────────────────────────────────────────────────────────────────┐
-- │  SQL Cookbook (Anthony Molinaro) helper tables.                    │
-- │  Load any time with:   .read data/seed_sqlcookbook.sql             │
-- │  (EMP comes separately from data/emp.xlsx — .import it if missing.) │
-- └──────────────────────────────────────────────────────────────────┘

-- DEPT — the classic 4-department table that EMP.DEPTNO joins to.
CREATE OR REPLACE TABLE dept (deptno INTEGER, dname VARCHAR, loc VARCHAR);
INSERT INTO dept VALUES
    (10, 'ACCOUNTING', 'NEW YORK'),
    (20, 'RESEARCH',   'DALLAS'),
    (30, 'SALES',      'CHICAGO'),
    (40, 'OPERATIONS', 'BOSTON');

-- Pivot/number tables. T<N> holds a single column `id` with values 1..N,
-- used across the book to generate rows of numbers for pivots.
CREATE OR REPLACE TABLE t1   AS SELECT * FROM range(1, 2)   AS t(id);
CREATE OR REPLACE TABLE t10  AS SELECT * FROM range(1, 11)  AS t(id);
CREATE OR REPLACE TABLE t100 AS SELECT * FROM range(1, 101) AS t(id);
CREATE OR REPLACE TABLE t500 AS SELECT * FROM range(1, 501) AS t(id);

-- Sanity check (prints when run through the watcher / .read):
SELECT 'dept' AS tbl, COUNT(*) AS rows FROM dept
UNION ALL SELECT 't1',   COUNT(*) FROM t1
UNION ALL SELECT 't10',  COUNT(*) FROM t10
UNION ALL SELECT 't100', COUNT(*) FROM t100
UNION ALL SELECT 't500', COUNT(*) FROM t500
ORDER BY tbl;
