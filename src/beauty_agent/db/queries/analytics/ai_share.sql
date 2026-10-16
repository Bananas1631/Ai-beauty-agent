-- Доля записей через ИИ и записей, сделанных вне рабочего времени администраторов.
-- Одна строка, все счётчики — агрегаты с FILTER за один проход по записям.
-- Период — по времени создания записи; отменённые не считаются.
--
-- Параметры: :salon_id, :period_start, :period_end (timestamptz), :tz,
--            :work_start, :work_end (time — рабочее время администраторов).
WITH created AS (
    SELECT
        created_by_ai,
        CAST(created_at AT TIME ZONE CAST(:tz AS text) AS time) AS local_time
    FROM bookings
    WHERE salon_id = CAST(:salon_id AS uuid)
      AND status <> 'cancelled'
      AND created_at >= CAST(:period_start AS timestamptz)
      AND created_at < CAST(:period_end AS timestamptz)
)
SELECT
    count(*) AS bookings,
    count(*) FILTER (WHERE created_by_ai) AS ai_bookings,
    coalesce(round(CAST(count(*) FILTER (WHERE created_by_ai) AS numeric) / nullif(count(*), 0), 3), 0)
        AS ai_share,
    count(*) FILTER (
        WHERE local_time < CAST(:work_start AS time) OR local_time >= CAST(:work_end AS time)
    ) AS off_hours_bookings,
    count(*) FILTER (
        WHERE created_by_ai AND (local_time < CAST(:work_start AS time) OR local_time >= CAST(:work_end AS time))
    ) AS off_hours_ai_bookings
FROM created;
