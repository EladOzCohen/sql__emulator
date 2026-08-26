-- ┌──────────────────────────────────────────────────────────────────┐
-- │  This is your scratch pad. Edit it, hit save, results appear in    │
-- │  the watcher window. Write SQL like normal SQL — no strings, no    │
-- │  backslashes, no wrapping it in a function.                        │
-- └──────────────────────────────────────────────────────────────────┘

-- Load the two sample CSVs into tables (safe to re-run — CREATE OR REPLACE).
CREATE OR REPLACE TABLE customers AS
    SELECT * FROM read_csv_auto('data/customers.csv', header = true);

CREATE OR REPLACE TABLE orders AS
    SELECT * FROM read_csv_auto('data/orders.csv', header = true);

-- Practice below. Every SELECT prints its own result table.

-- Total spend per city, most valuable first:
SELECT
    c.city,
    count(DISTINCT c.customer_id) AS customers,
    count(o.order_id)             AS orders,
    round(sum(o.amount), 2)       AS revenue
FROM customers AS c
LEFT JOIN orders  AS o ON o.customer_id = c.customer_id
GROUP BY c.city
ORDER BY revenue DESC;

-- Each customer's biggest single order (window function):
SELECT name, product, amount
FROM (
    SELECT
        c.name,
        o.product,
        o.amount,
        row_number() OVER (PARTITION BY c.customer_id ORDER BY o.amount DESC) AS rn
    FROM customers AS c
    JOIN orders   AS o ON o.customer_id = c.customer_id
)
WHERE rn = 1
ORDER BY amount DESC;
