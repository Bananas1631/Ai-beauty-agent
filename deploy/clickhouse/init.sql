-- Аналитическое хранилище (шаг 24). Выполняется при первом старте контейнера ClickHouse
-- (/docker-entrypoint-initdb.d) и в тестах (analytics/clickhouse.py: apply_schema).
--
-- Kafka → таблица на движке Kafka → материализованное представление → analytics.events.
-- Кода на Python в этом пути нет: ClickHouse сам читает топики своей группой потребителей
-- clickhouse (независимо от групп сервиса) и коммитит offset после записи блока.
-- Персональных данных нет и в событиях: только ID, типы, суммы, токены и задержки.

CREATE DATABASE IF NOT EXISTS analytics;

-- Одна строка на доменное событие. ReplacingMergeTree схлопывает дубли доставки at-least-once
-- (одинаковый ключ сортировки, в него входит event_id) при слияниях частей — «когда-нибудь».
-- Отчёты, где важна точность (деньги, счёт), читают с FINAL.
-- ORDER BY (salon_id, event_type, ts, event_id): почти каждый запрос — один салон и один тип события
-- за период; разреженный индекс по этому префиксу отсекает почти все гранулы.
CREATE TABLE IF NOT EXISTS analytics.events
(
    event_id          UUID,
    event_type        LowCardinality(String),
    schema_version    UInt8,
    salon_id          UUID,
    ts                DateTime64(3, 'UTC'),
    conversation_id   Nullable(UUID),
    booking_id        Nullable(UUID),
    client_id         Nullable(UUID),
    channel           LowCardinality(String),
    status            LowCardinality(String),
    created_by_ai     Bool,
    price             Nullable(Decimal(12, 2)),
    model             LowCardinality(String),
    prompt_tokens     UInt32,
    completion_tokens UInt32,
    cost_rub          Decimal(12, 4),
    latency_ms        UInt32,
    tool_calls        UInt16,
    offers_shown      UInt16,
    cache_hit         Bool,
    handed_off        Bool,
    billing_month     LowCardinality(String),
    ingested_at       DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(ts)
ORDER BY (salon_id, event_type, ts, event_id)
TTL toDateTime(ts) + INTERVAL 730 DAY;

-- События, которые не удалось разобрать: не JSON, нет event_id, salon_id или времени.
-- Без этой таблицы кривое сообщение остановило бы приём: блок с ошибкой не вставляется,
-- и движок Kafka читал бы его снова и снова. Ошибку разбора отдаёт сам движок
-- (kafka_handle_error_mode = 'stream': виртуальные колонки _error и _raw_message).
CREATE TABLE IF NOT EXISTS analytics.events_errors
(
    raw         String,
    received_at DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY received_at
TTL toDateTime(received_at) + INTERVAL 30 DAY;

-- Очередь: каждое сообщение Kafka — одна строка JSON. Брокер — внутри сети Docker.
-- Топик knowledge (обмен с индексатором) не читаем: аналитике он не нужен.
CREATE TABLE IF NOT EXISTS analytics.events_queue
(
    raw String
)
ENGINE = Kafka
SETTINGS
    kafka_broker_list = 'kafka:9092',
    kafka_topic_list = 'beauty.bookings,beauty.messages,beauty.llm',
    kafka_group_name = 'clickhouse',
    kafka_format = 'JSONAsString',
    kafka_num_consumers = 1,
    kafka_poll_timeout_ms = 500,
    kafka_flush_interval_ms = 1000,
    kafka_handle_error_mode = 'stream';

CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.events_mv TO analytics.events AS
SELECT
    toUUID(JSONExtractString(raw, 'event_id')) AS event_id,
    JSONExtractString(raw, 'type') AS event_type,
    toUInt8(JSONExtractUInt(raw, 'schema_version')) AS schema_version,
    toUUID(JSONExtractString(raw, 'salon_id')) AS salon_id,
    parseDateTime64BestEffort(JSONExtractString(raw, 'occurred_at'), 3, 'UTC') AS ts,
    toUUIDOrNull(JSONExtractString(raw, 'data', 'conversation_id')) AS conversation_id,
    toUUIDOrNull(JSONExtractString(raw, 'data', 'booking_id')) AS booking_id,
    toUUIDOrNull(JSONExtractString(raw, 'data', 'client_id')) AS client_id,
    JSONExtractString(raw, 'data', 'channel') AS channel,
    JSONExtractString(raw, 'data', 'status') AS status,
    JSONExtractBool(raw, 'data', 'created_by_ai') AS created_by_ai,
    -- деньги в событиях — строки ("1500.00"): Decimal без потери точности, не float
    toDecimal64OrNull(trim(BOTH '"' FROM JSONExtractRaw(raw, 'data', 'price')), 2) AS price,
    JSONExtractString(raw, 'data', 'model') AS model,
    toUInt32(JSONExtractUInt(raw, 'data', 'prompt_tokens')) AS prompt_tokens,
    toUInt32(JSONExtractUInt(raw, 'data', 'completion_tokens')) AS completion_tokens,
    toDecimal64OrZero(trim(BOTH '"' FROM JSONExtractRaw(raw, 'data', 'cost_rub')), 4) AS cost_rub,
    toUInt32(JSONExtractUInt(raw, 'data', 'latency_ms')) AS latency_ms,
    toUInt16(JSONExtractUInt(raw, 'data', 'tool_calls')) AS tool_calls,
    toUInt16(JSONExtractUInt(raw, 'data', 'offers_shown')) AS offers_shown,
    JSONExtractBool(raw, 'data', 'cache_hit') AS cache_hit,
    JSONExtractBool(raw, 'data', 'handed_off') AS handed_off,
    JSONExtractString(raw, 'data', 'billing_month') AS billing_month
FROM analytics.events_queue
WHERE length(_error) = 0
  AND isValidJSON(raw)
  AND toUUIDOrNull(JSONExtractString(raw, 'event_id')) IS NOT NULL
  AND toUUIDOrNull(JSONExtractString(raw, 'salon_id')) IS NOT NULL
  AND parseDateTime64BestEffortOrNull(JSONExtractString(raw, 'occurred_at'), 3, 'UTC') IS NOT NULL;

CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.events_errors_mv TO analytics.events_errors AS
SELECT if(length(_error) > 0, _raw_message, raw) AS raw
FROM analytics.events_queue
WHERE length(_error) > 0 OR NOT (
    isValidJSON(raw)
    AND toUUIDOrNull(JSONExtractString(raw, 'event_id')) IS NOT NULL
    AND toUUIDOrNull(JSONExtractString(raw, 'salon_id')) IS NOT NULL
    AND parseDateTime64BestEffortOrNull(JSONExtractString(raw, 'occurred_at'), 3, 'UTC') IS NOT NULL
);
