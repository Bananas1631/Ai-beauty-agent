-- Загрузка мастеров по часам недели: доля занятых минут в каждой клетке «день недели × час».
-- Сетка строится из generate_series, записи присоединяются LEFT JOIN: мастер без записей
-- остаётся в отчёте с нулями, а не пропадает. Время — по часовому поясу салона.
-- Запись 10:30–12:00 даёт 30 минут в клетке 10:00 и 60 минут в клетке 11:00.
--
-- Параметры: :salon_id, :period_start, :period_end (timestamptz, конец не включается),
--            :tz (часовой пояс салона), :open_hour, :close_hour (рабочие часы салона).
WITH masters_of_salon AS (
    SELECT id, name
    FROM masters
    WHERE salon_id = CAST(:salon_id AS uuid) AND is_active
),
grid AS (
    SELECT m.id AS master_id, m.name AS master_name, d.dow, h.hour
    FROM masters_of_salon AS m
    CROSS JOIN generate_series(1, 7) AS d (dow)
    CROSS JOIN generate_series(CAST(:open_hour AS integer), CAST(:close_hour AS integer) - 1) AS h (hour)
),
days AS (
    -- сколько раз каждый день недели встречается в периоде: ёмкость клетки = дни × 60 минут
    SELECT CAST(extract(isodow FROM day) AS integer) AS dow, count(*) AS n
    FROM generate_series(
        CAST(CAST(:period_start AS timestamptz) AT TIME ZONE CAST(:tz AS text) AS date),
        CAST(CAST(:period_end AS timestamptz) AT TIME ZONE CAST(:tz AS text) AS date) - 1,
        interval '1 day'
    ) AS day
    GROUP BY 1
),
local_bookings AS MATERIALIZED (
    -- перевод в пояс салона и число часовых клеток — один раз на запись, а не на каждую клетку
    SELECT
        master_id,
        local_start,
        local_end,
        date_trunc('hour', local_start) AS first_cell,
        CAST(ceil(extract(epoch FROM local_end - date_trunc('hour', local_start)) / 3600) AS integer) AS cells
    FROM (
        SELECT
            master_id,
            starts_at AT TIME ZONE CAST(:tz AS text) AS local_start,
            ends_at AT TIME ZONE CAST(:tz AS text) AS local_end
        FROM bookings
        WHERE salon_id = CAST(:salon_id AS uuid)
          AND status <> 'cancelled'
          AND starts_at < CAST(:period_end AS timestamptz)
          AND ends_at > CAST(:period_start AS timestamptz)
    ) AS raw
),
busy AS (
    -- Клетки записи — смещения от часа начала в пределах её длины. Не generate_series по каждой
    -- записи: для такой функции планировщик предполагает 1000 строк на запись и уходит
    -- в параллельный план, который дороже самой работы (docs/performance.md).
    SELECT
        b.master_id,
        CAST(extract(isodow FROM c.cell) AS integer) AS dow,
        CAST(extract(hour FROM c.cell) AS integer) AS hour,
        sum(extract(epoch FROM least(b.local_end, c.cell + interval '1 hour') - greatest(b.local_start, c.cell)) / 60)
            AS minutes
    FROM local_bookings AS b
    JOIN generate_series(0, 23) AS k (offset_hours) ON k.offset_hours < b.cells
    CROSS JOIN LATERAL (SELECT b.first_cell + make_interval(hours => k.offset_hours) AS cell) AS c
    GROUP BY 1, 2, 3
)
SELECT
    g.master_id,
    g.master_name,
    g.dow,
    g.hour,
    CAST(coalesce(bz.minutes, 0) AS numeric(10, 1)) AS booked_minutes,
    coalesce(d.n, 0) * 60 AS capacity_minutes,
    CASE
        WHEN coalesce(d.n, 0) = 0 THEN 0
        ELSE round(CAST(coalesce(bz.minutes, 0) / (d.n * 60) AS numeric), 3)
    END AS load
FROM grid AS g
LEFT JOIN days AS d ON d.dow = g.dow
LEFT JOIN busy AS bz ON bz.master_id = g.master_id AND bz.dow = g.dow AND bz.hour = g.hour
ORDER BY g.master_name, g.master_id, g.dow, g.hour;
