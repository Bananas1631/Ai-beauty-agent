# beauty-indexer

Сервис индексации сайтов салонов (ADR 014): обходит сайт, режет страницы на фрагменты, считает
эмбеддинги и хранит их в своей схеме `knowledge`; основному сервису отдаёт гибридный поиск по REST.

```
основной сервис ──site.reindex.requested──▶ Kafka ──▶ indexer-worker: задача → обход → outbox
       ▲                                                         │
       └──────────────knowledge.updated◀── Kafka ◀── relay ◀─────┘
агент ──POST /v1/salons/{id}/search (OAuth2, knowledge:read)──▶ indexer API
```

## Запуск

Из корня проекта, общий `poetry install`:

```bash
poetry run alembic -c services/indexer/alembic.ini upgrade head      # схема knowledge
PYTHONPATH=services/indexer poetry run uvicorn beauty_indexer.api:create_app --factory --port 8001
PYTHONPATH=services/indexer poetry run python -m beauty_indexer.worker
```

В Docker — сервисы `indexer` и `indexer-worker` в `deploy/docker-compose.yml` (`make up`, `make migrate`).
Агент начинает пользоваться индексатором при `KNOWLEDGE_BACKEND=shadow` (сравнение) или `indexer`.

## Настройки

| Переменная | Что | По умолчанию |
| --- | --- | --- |
| `INDEXER_JWKS_URL` | открытые ключи основного сервиса для проверки токенов | `http://localhost:8000/.well-known/jwks.json` |
| `INDEXER_MAX_PAGES` | страниц на один обход | 30 |
| `INDEXER_PAUSE_S` | пауза между страницами (вежливый обход) | 0.5 |
| `INDEXER_STALE_JOB_S` | через сколько «running» задача считается брошенной | 1800 |
| `POSTGRES_*`, `KAFKA_*`, `LLM_*` | те же, что у основного сервиса | — |

## API

| Метод | Что | Права |
| --- | --- | --- |
| `POST /v1/salons/{salon_id}/search` | `{"question", "k"}` → `{"version", "fragments": [{chunk_id, title, url, content, score, distance, text_match}]}` | `knowledge:read` |
| `GET /v1/salons/{salon_id}/version` | версия базы знаний сайта (меняется с переиндексацией изменённых страниц) | `knowledge:read` |
| `GET /health`, `GET /ready` | процесс жив / база доступна | — |

Ответ `/search` — контракт с основным сервисом: поля можно добавлять, нельзя удалять и переименовывать
(`tests/contract/test_indexer_api.py`).

## Тесты

```bash
poetry run pytest services/indexer/beauty_indexer/tests
```

Свой PostgreSQL в контейнере только со схемой `knowledge` — индексатор не зависит от таблиц
основного сервиса.
