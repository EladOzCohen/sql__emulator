-- ┌──────────────────────────────────────────────────────────────────┐
-- │  MySQL-flavored scratch pad — runs locally on DuckDB. Edit + save  │
-- │  and the watcher re-runs. Type .dialect in the REPL for the full   │
-- │  cheat-sheet of differences (backticks, LIMIT offset, etc.).       │
-- └──────────────────────────────────────────────────────────────────┘

CREATE OR REPLACE TABLE customers AS
    SELECT * FROM read_csv_auto('data/customers.csv', header = true);
CREATE OR REPLACE TABLE orders AS
    SELECT * FROM read_csv_auto('data/orders.csv', header = true);

-- DATE_FORMAT + IFNULL (from the MySQL prelude), revenue per month:
SELECT
    DATE_FORMAT(order_date, '%Y-%m')  AS month,
    COUNT(*)                          AS orders,
    IFNULL(ROUND(SUM(amount), 2), 0)  AS revenue
FROM orders
GROUP BY month
ORDER BY month;

-- Customers and their total spend, biggest first:
SELECT
    c.name,
    c.city,
    COUNT(o.order_id)        AS orders,
    ROUND(SUM(o.amount), 2)  AS total_spend
FROM customers AS c
LEFT JOIN orders  AS o ON o.customer_id = c.customer_id
GROUP BY c.name, c.city
ORDER BY total_spend DESC;
