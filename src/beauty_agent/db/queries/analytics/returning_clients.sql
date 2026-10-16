-- Возвращаемость: интервал между визитами клиента (LAG) и клиенты, которые не приходили
-- дольше двух своих обычных интервалов, — кандидаты на «давно не были, запишем?».
-- Клиент с одним визитом обычного интервала не имеет и в список не попадает.
-- Визит — запись «подтверждена» или «состоялась», время которой уже прошло.
--
-- Параметры: :salon_id, :as_of (timestamptz — «сейчас» отчёта), :limit.
WITH visits AS (
    SELECT
        client_id,
        starts_at,
        starts_at - lag(starts_at) OVER (PARTITION BY client_id ORDER BY starts_at) AS gap
    FROM bookings
    WHERE salon_id = CAST(:salon_id AS uuid)
      AND client_id IS NOT NULL
      AND status IN ('confirmed', 'completed')
      AND starts_at < CAST(:as_of AS timestamptz)
),
per_client AS (
    SELECT
        client_id,
        count(*) AS visits,
        max(starts_at) AS last_visit,
        avg(gap) AS usual_gap  -- первый визит без предыдущего даёт NULL, avg его пропускает
    FROM visits
    GROUP BY client_id
)
SELECT
    p.client_id,
    c.name AS client_name,
    p.visits,
    p.last_visit,
    round(CAST(extract(epoch FROM p.usual_gap) / 86400 AS numeric), 1) AS usual_interval_days,
    round(CAST(extract(epoch FROM CAST(:as_of AS timestamptz) - p.last_visit) / 86400 AS numeric), 1)
        AS days_since_last
FROM per_client AS p
JOIN clients AS c ON c.id = p.client_id
WHERE p.visits >= 2
  AND CAST(:as_of AS timestamptz) - p.last_visit > 2 * p.usual_gap
ORDER BY days_since_last DESC, p.client_id
LIMIT CAST(:limit AS integer);
