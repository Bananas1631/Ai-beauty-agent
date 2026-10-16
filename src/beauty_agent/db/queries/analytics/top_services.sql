-- Топ услуг по выручке: доля в выручке салона и накопительный итог (SUM OVER) — видно,
-- какие услуги дают 80% денег. Доля считается от всей выручки, а не только от показанных строк:
-- окна вычисляются до LIMIT.
--
-- Параметры: :salon_id, :period_start, :period_end (timestamptz), :limit.
WITH revenue AS (
    SELECT s.id AS service_id, s.name AS service_name, count(*) AS visits, sum(b.price) AS revenue
    FROM bookings AS b
    JOIN services AS s ON s.id = b.service_id
    WHERE b.salon_id = CAST(:salon_id AS uuid)
      AND b.status IN ('confirmed', 'completed')
      AND b.price IS NOT NULL
      AND b.starts_at >= CAST(:period_start AS timestamptz)
      AND b.starts_at < CAST(:period_end AS timestamptz)
    GROUP BY s.id, s.name
)
SELECT
    service_id,
    service_name,
    visits,
    revenue,
    round(revenue / sum(revenue) OVER (), 4) AS share,
    sum(revenue) OVER running AS cumulative_revenue,
    round(sum(revenue) OVER running / sum(revenue) OVER (), 4) AS cumulative_share
FROM revenue
WINDOW running AS (ORDER BY revenue DESC, service_name, service_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
ORDER BY revenue DESC, service_name, service_id
LIMIT CAST(:limit AS integer);
