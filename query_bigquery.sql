-- ┌──────────────────────────────────────────────────────────────────┐
-- │  BigQuery (GoogleSQL) scratch pad — runs locally on DuckDB with a  │
-- │  compatibility prelude, so most GoogleSQL just works. Edit + save  │
-- │  and the watcher re-runs. Type .dialect in the REPL for the full   │
-- │  cheat-sheet of what's native, what's emulated, and what differs.  │
-- └──────────────────────────────────────────────────────────────────┘

CREATE OR REPLACE TABLE customers AS
    SELECT * FROM read_csv_auto('data/customers.csv', header = true);
CREATE OR REPLACE TABLE orders AS
    SELECT * FROM read_csv_auto('data/orders.csv', header = true);

-- Revenue by month: FORMAT_DATE + SAFE_DIVIDE (both from the BigQuery prelude),
-- grouping by the SELECT alias (GoogleSQL style).
SELECT
    FORMAT_DATE('%Y-%m', order_date)              AS month,
    COUNT(*)                                      AS orders,
    ROUND(SUM(amount), 2)                         AS revenue,
    ROUND(SAFE_DIVIDE(SUM(amount), COUNT(*)), 2)  AS avg_order
FROM orders
GROUP BY month
ORDER BY month;

-- Top order per customer with QUALIFY (very BigQuery; works natively):
SELECT
    c.name,
    o.product,
    o.amount
FROM customers AS c
JOIN orders   AS o USING (customer_id)
QUALIFY ROW_NUMBER() OVER (PARTITION BY c.customer_id ORDER BY o.amount DESC) = 1
ORDER BY o.amount DESC;

-- COUNT_IF + category share, and REGEXP_CONTAINS filter:
SELECT
    category,
    COUNT(*)                                 AS n,
    COUNT_IF(amount > 300)                    AS big_orders
FROM orders
WHERE REGEXP_CONTAINS(product, '(?i)monitor|keyboard')
GROUP BY category
ORDER BY n DESC;

-- Arrays: GENERATE_ARRAY + UNNEST.
-- NOTE: DuckDB needs "AS t(n)"; real BigQuery would write "AS n".
SELECT n, n * n AS squared
FROM UNNEST(GENERATE_ARRAY(1, 5)) AS t(n)
ORDER BY n;
