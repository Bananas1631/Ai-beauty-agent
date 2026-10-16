# Каталог доменных событий

События — «это произошло»: их публикует сервис, где факт случился, и читает любой, кому он нужен,
своей группой потребителей. Задачи («сделай это») — в Celery и RabbitMQ. Почему так — ADR 013.

## Как устроено

- **Схемы** — Pydantic-модели в `src/beauty_agent/domain/events.py`; JSON Schema для других сервисов
  (биллинг на Kotlin, шаг 29) — в `docs/events/schemas/`, выгрузка `python -m scripts.export_event_schemas`.
  Тест падает, если выгрузка устарела.
- **Конверт** каждого события: `event_id` (UUID, идемпотентность потребителей), `type`,
  `schema_version`, `salon_id` (ключ сообщения), `occurred_at` (UTC), `data`.
- **Персональных данных нет**: в `data` только ID, время, суммы и перечисления. Имя, телефон
  и текст сообщения в журнал не попадают.
- **Публикация** — transactional outbox: строка в `event_outbox` в той же транзакции, что и данные;
  relay (`events/outbox_relay.py`) публикует по порядку внутри салона и отмечает. Гарантия —
  at-least-once: потребитель обязан отсеивать дубли по `event_id`.
- **Заголовки** сообщения Kafka: `event_type`, `event_id`, `schema_version` — чтобы отсеять чужое,
  не разбирая тело.
- **Версии.** Новое поле — со значением по умолчанию в той же версии. Удалить поле или сменить его
  смысл — новая версия схемы: новый класс с `schema_version = 2`, старые потребители продолжают
  читать первую, продюсер какое-то время публикует обе.

## Топики

| Топик | Ключ | Партиций | Хранение | События |
| --- | --- | --- | --- | --- |
| `beauty.bookings` | salon_id | 6 | 7 дней | booking.created, booking.rescheduled, booking.cancelled, booking.attributed |
| `beauty.messages` | salon_id | 6 | 7 дней | message.received |
| `beauty.llm` | salon_id | 6 | 7 дней | llm.call.finished |
| `<топик>.dlq` | как в исходном | 6 | 7 дней | события, которые потребитель не смог обработать за 3 попытки, и сообщения не по схеме; причина — в заголовках `dlq_reason`, `dlq_group`, `dlq_source` |

Топики создаёт relay при старте (`events/producer.py: ensure_topics`); автосоздание в брокере выключено.

## События

| Тип | Версия | Кто публикует | Когда | Данные | Кто читает |
| --- | --- | --- | --- | --- | --- |
| `booking.created` | 1 | `SqlBookingLedger.record` | запись сохранена (ИИ записал клиента) | booking_id, external_id, client_id, conversation_id, service_id, master_id, starts_at, ends_at, price, status, created_by_ai | weekly-stats (ai_bookings); ClickHouse (шаг 24) |
| `booking.rescheduled` | 1 | `SqlBookingLedger.move` | запись перенесена (агентом или в YCLIENTS) | то же + previous_starts_at, previous_master_id | ClickHouse |
| `booking.cancelled` | 1 | `SqlBookingLedger.cancel` | запись отменена | как booking.created, статус cancelled | weekly-stats (cancelled_bookings); ClickHouse |
| `booking.attributed` | 1 | `workers/attribution.py` (каждые 15 минут) | визит наступил, запись засчитана ИИ по правилу `domain/billing.py`; одно событие на запись | booking_id, starts_at, price, status, billing_month | weekly-stats (attributed_visits); биллинг на Kotlin (шаг 29) |
| `message.received` | 1 | `ConversationService` (accept, handle) | сообщение клиента сохранено | message_id, conversation_id, client_id, channel | weekly-stats (messages_received) |
| `llm.call.finished` | 1 | `ConversationService` | ход агента закончен, ответ сохранён | conversation_id, model (null — ответ без модели: кэш, guardrail), prompt_version, prompt/completion_tokens, cost_rub, latency_ms, tool_calls, cache_hit, handed_off, guardrail | weekly-stats (answers, off_hours_answers, llm_cost_rub); ClickHouse |

## Группы потребителей

| Группа | Код | Топики | Что делает |
| --- | --- | --- | --- |
| `weekly-stats` | `events/handlers/reports.py` | все три | копит `salon_week_stats`; еженедельный отчёт берёт цифры оттуда при `KAFKA_ENABLED=true` |

Новый потребитель — обработчик в `events/handlers/` и строка в `events/consumers.py`;
запуск `python -m beauty_agent.events.consumers <группа>`, несколько экземпляров делят партиции.

## Эксплуатация

- **Перечитать**: `python -m scripts.replay_events <обработчик> [--since …] [--offset топик:партиция:offset]` —
  своя группа Kafka, уже применённое пропускается (идемпотентность за обработчиком).
- **Бэкап Kafka не нужен**: источник правды — PostgreSQL. Опубликованное хранится в `event_outbox`
  7 дней, неопубликованное — пока relay его не опубликует.
- **Метрики**: `beauty_agent_event_outbox_backlog`, `beauty_agent_consumer_lag{group,topic}`,
  `beauty_agent_events_dead_lettered_total`; алерты — растущий lag 10 минут и события в DLQ
  (что делать — `docs/runbook.md`).
- **Kafka UI**: `docker compose -f deploy/docker-compose.yml --profile events up -d`, http://localhost:8080.
