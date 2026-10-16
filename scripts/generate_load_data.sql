-- Данные масштаба 500 салонов для планов запросов и нагрузочных тестов — одним SQL, без циклов в Python.
-- Только для отдельной базы: НЕ запускать на базе пилотов. Схема — после `alembic upgrade head`.
--
--   psql -v scale=1 -f scripts/generate_load_data.sql      -- 500 салонов, ~3–4 ГБ
--   psql -v scale=0.1 -f scripts/generate_load_data.sql    -- 50 салонов, ~0,4 ГБ (ноутбук)
--
-- Масштаб меняет число салонов, а не размер салона: в каждом 8 мастеров, 20 услуг, 2 000 клиентов,
-- ~10 000 записей за три года и ~20 000 сообщений — запросы одного салона работают как в жизни.
-- При scale=1: 1 млн клиентов, ~5 млн записей, 10 млн сообщений.
--
-- Распределение как у настоящего салона: пятница и суббота загружены, воскресенье и понедельник —
-- меньше; пики в обед и после 17:00. Записи стоят на сетке «мастер × день × час», поэтому
-- ограничение «один мастер — одна запись в момент времени» не нарушается.
-- ID детерминированные (md5 от номера): повторный запуск на чистой базе даёт те же данные.

\set ON_ERROR_STOP on
\if :{?scale}
\else
    \set scale 1
\endif
\set density 0.155

SELECT set_config('load.salons', greatest(1, round(500 * :scale))::text, false);
SELECT setseed(0.42);

-- салоны и каталог -------------------------------------------------------------------------
INSERT INTO salons (id, slug, name, timezone, settings)
SELECT md5('salon-' || n)::uuid, 'load-' || n, 'Салон нагрузки ' || n, 'Europe/Moscow', '{"crm_kind": "fake"}'
FROM generate_series(1, current_setting('load.salons')::int) AS n;

INSERT INTO masters (id, salon_id, external_id, name)
SELECT md5('master-' || n || '-' || k)::uuid, md5('salon-' || n)::uuid, k::text, 'Мастер ' || k
FROM generate_series(1, current_setting('load.salons')::int) AS n, generate_series(1, 8) AS k;

INSERT INTO services (id, salon_id, external_id, name, category, price_min, price_max, duration_min)
SELECT md5('service-' || n || '-' || k)::uuid, md5('salon-' || n)::uuid, k::text, 'Услуга ' || k,
       'Категория ' || (k % 4), 1000 + k * 150, 1000 + k * 150, 60
FROM generate_series(1, current_setting('load.salons')::int) AS n, generate_series(1, 20) AS k;

INSERT INTO clients (id, salon_id, name, phone)
SELECT md5('client-' || n || '-' || k)::uuid, md5('salon-' || n)::uuid, 'Клиент ' || k,
       '+79' || lpad((n * 10000 + k)::text, 9, '0')
FROM generate_series(1, current_setting('load.salons')::int) AS n, generate_series(1, 2000) AS k;

-- записи: сетка «мастер × день × час» за три года, вероятность занятости по дню недели и часу ------
INSERT INTO bookings (
    id, salon_id, master_id, service_id, client_id, starts_at, ends_at, status, price,
    idempotency_key, created_by_ai, created_at, updated_at
)
SELECT
    md5('booking-' || g.n || '-' || g.k || '-' || g.day || '-' || g.hour)::uuid,
    md5('salon-' || g.n)::uuid,
    md5('master-' || g.n || '-' || g.k)::uuid,
    md5('service-' || g.n || '-' || g.service)::uuid,
    md5('client-' || g.n || '-' || g.client)::uuid,
    g.starts_at,
    g.starts_at + interval '50 minutes',
    CASE
        WHEN g.starts_at > now() THEN (CASE WHEN g.r < 0.9 THEN 'confirmed' ELSE 'cancelled' END)
        WHEN g.r < 0.85 THEN 'completed'
        WHEN g.r < 0.93 THEN 'cancelled'
        WHEN g.r < 0.97 THEN 'no_show'
        ELSE 'confirmed'
    END,
    1000 + g.service * 150,
    md5('key-' || g.n || '-' || g.k || '-' || g.day || '-' || g.hour),
    g.r2 < 0.3,
    g.starts_at - (g.r3 * interval '14 days'),
    g.starts_at - (g.r3 * interval '14 days')
FROM (
    SELECT
        n, k, day, hour,
        (day + make_interval(hours => hour)) AT TIME ZONE 'Europe/Moscow' AS starts_at,
        1 + floor(random() * 20)::int AS service,
        1 + floor(random() * 2000)::int AS client,
        random() AS r, random() AS r2, random() AS r3, random() AS pick,
        CASE extract(isodow FROM day)
            WHEN 1 THEN 0.70 WHEN 2 THEN 0.80 WHEN 3 THEN 0.85 WHEN 4 THEN 0.90
            WHEN 5 THEN 1.00 WHEN 6 THEN 1.00 ELSE 0.60
        END
        * CASE hour
            WHEN 10 THEN 0.50 WHEN 11 THEN 0.80 WHEN 12 THEN 0.90 WHEN 13 THEN 0.80 WHEN 14 THEN 0.70
            WHEN 15 THEN 0.70 WHEN 16 THEN 0.80 WHEN 17 THEN 1.00 WHEN 18 THEN 1.00 WHEN 19 THEN 0.90
            ELSE 0.60
        END AS weight
    FROM generate_series(1, current_setting('load.salons')::int) AS n,
         generate_series(1, 8) AS k,
         generate_series(
             date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') - interval '1065 days',
             date_trunc('day', now() AT TIME ZONE 'Europe/Moscow') + interval '30 days',
             interval '1 day'
         ) AS day,
         generate_series(10, 20) AS hour
) AS g
WHERE g.pick < g.weight * :density;

-- диалоги и сообщения: диалог на клиента, 10 сообщений в каждом -----------------------------------
INSERT INTO conversations (id, salon_id, client_id, channel, status, created_at, updated_at, last_message_at)
SELECT md5('conversation-' || n || '-' || k)::uuid, md5('salon-' || n)::uuid, md5('client-' || n || '-' || k)::uuid,
       (ARRAY['telegram', 'vk', 'max', 'web'])[1 + (k % 4)], 'active',
       now() - random() * interval '1095 days', now(), now()
FROM generate_series(1, current_setting('load.salons')::int) AS n, generate_series(1, 2000) AS k;

INSERT INTO messages (id, salon_id, conversation_id, role, content, created_at, updated_at)
SELECT md5('message-' || c.id || '-' || m)::uuid, c.salon_id, c.id,
       CASE WHEN m % 2 = 1 THEN 'client' ELSE 'agent' END,
       CASE WHEN m % 2 = 1 THEN 'Здравствуйте, хочу записаться на маникюр в пятницу вечером'
            ELSE 'Добрый день! Есть свободное время в пятницу в 18:00 и в 19:00 у мастера Ольги.' END,
       c.created_at + m * interval '3 minutes', c.created_at + m * interval '3 minutes'
FROM conversations AS c, generate_series(1, 10) AS m
WHERE c.salon_id IN (SELECT md5('salon-' || n)::uuid FROM generate_series(1, current_setting('load.salons')::int) AS n);

UPDATE conversations AS c
SET last_message_at = c.created_at + interval '30 minutes'
WHERE c.salon_id IN (SELECT md5('salon-' || n)::uuid FROM generate_series(1, current_setting('load.salons')::int) AS n);

-- один длинный диалог (100 000 сообщений) — для сравнения keyset-пагинации и OFFSET --------------
INSERT INTO messages (id, salon_id, conversation_id, role, content, created_at, updated_at)
SELECT md5('long-' || m)::uuid, md5('salon-1')::uuid, md5('conversation-1-1')::uuid,
       CASE WHEN m % 2 = 1 THEN 'client' ELSE 'agent' END, 'Сообщение длинной истории ' || m,
       now() - interval '1095 days' + m * interval '5 minutes', now()
FROM generate_series(1, 100000) AS m;

ANALYZE;

SELECT 'salons' AS table_name, count(*) FROM salons
UNION ALL SELECT 'clients', count(*) FROM clients
UNION ALL SELECT 'bookings', count(*) FROM bookings
UNION ALL SELECT 'conversations', count(*) FROM conversations
UNION ALL SELECT 'messages', count(*) FROM messages;
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;
