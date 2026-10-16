-- Выручка и средний чек по неделям в часовом поясе салона.
-- Неделя без визитов тоже есть в отчёте — с нулями: недели строит generate_series.
-- Выручка — визиты «подтверждена» и «состоялась» с известной ценой; отмены и неявки не считаются.
-- Запись в 00:30 по Москве 1 декабря — это 21:30 30 ноября по UTC: неделя считается по Москве.
--
-- Параметры: :salon_id, :period_start, :period_end (timestamptz), :tz.
WITH weeks AS (
    SELECT CAST(week AS date) AS week
    FROM generate_series(
        date_trunc('week', CAST(:period_start AS timestamptz) AT TIME ZONE CAST(:tz AS text)),
        date_trunc('week', CAST(:period_end AS timestamptz) AT TIME ZONE CAST(:tz AS text) - interval '1 microsecond'),
        interval '1 week'
    ) AS week
),
visits AS (
    SELECT
        CAST(date_trunc('week', starts_at AT TIME ZONE CAST(:tz AS text)) AS date) AS week,
        price
    FROM bookings
    WHERE salon_id = CAST(:salon_id AS uuid)
      AND status IN ('confirmed', 'completed')
      AND price IS NOT NULL
      AND starts_at >= CAST(:period_start AS timestamptz)
      AND starts_at < CAST(:period_end AS timestamptz)
)
SELECT
    w.week,
    count(v.price) AS visits,
    coalesce(sum(v.price), 0) AS revenue,
    round(avg(v.price), 2) AS average_check
FROM weeks AS w
LEFT JOIN visits AS v ON v.week = w.week
GROUP BY w.week
ORDER BY w.week;
