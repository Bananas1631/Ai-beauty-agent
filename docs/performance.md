# Производительность

Документ пополняется: SQL-аналитика (шаг 18), нагрузочный тест (шаг 19), PostgreSQL против
ClickHouse (шаг 24), профилирование (шаг 26).

## Аналитика салона: планы до и после индексов (1 декабря 2026)

### Данные и стенд

- Генератор: [`scripts/generate_load_data.sql`](../scripts/generate_load_data.sql) — один SQL на
  `generate_series`, распределение по дням недели и часам как у салона (пятница и суббота
  загружены, пики в обед и после 17:00).
- Масштаб: **0,1** — 50 салонов, 100 000 клиентов, 494 507 записей за 3 года, 100 000 диалогов,
  1,1 млн сообщений, база 685 МБ, генерация — 70 секунд. Полный масштаб (500 салонов, ~5 млн
  записей, 10 млн сообщений, 3–4 ГБ) на ноутбуке не запускался: на диске Docker было 9 ГБ свободных,
  а виртуальный диск Docker Desktop после удаления данных сам не сжимается. Запуск —
  `psql -v scale=1` на сервере. Масштаб меняет число салонов, а не размер салона: в каждом 8 мастеров,
  2 000 клиентов и ~10 000 записей, поэтому запросы одного салона на полном масштабе читают те же
  строки — меняется только размер индексов по `salon_id`.
- PostgreSQL 17 (pgvector:pg17) в Docker Desktop на Windows, `shared_buffers=256MB`, `work_mem=16MB`,
  данные в кэше (shared hit). Время — медиана 5 запусков с клиента (asyncpg) и `Execution Time`
  из `EXPLAIN (ANALYZE, BUFFERS)`. Замеры: [`scripts/explain_analytics.py`](../scripts/explain_analytics.py).

### Что нашёл EXPLAIN

Записи одного салона за 3 года лежат в ~950 блоках таблицы вперемешку с записями других салонов
(они создаются день за днём). Единственный подходящий индекс — `ix_bookings_salon_id`: запрос
за 90 дней читал **все** записи салона и отбрасывал 92% фильтром по времени
(`Rows Removed by Filter: 9198` на 743 нужные строки), 1 311 буферов на любой период.

### Решения

| Индекс | Тип | Для чего |
| --- | --- | --- |
| `ix_bookings_salon_period (salon_id, starts_at) INCLUDE (ends_at, master_id, service_id, client_id, price, status) WHERE status <> 'cancelled'` | составной, частичный, покрывающий | загрузка мастеров, выручка, топ услуг — Index Only Scan только по периоду |
| `ix_bookings_salon_created (salon_id, created_at) INCLUDE (created_by_ai, status) WHERE status <> 'cancelled'` | составной, частичный, покрывающий | доля записей ИИ по времени создания |
| `ix_bookings_salon_client_visits (salon_id, client_id, starts_at) WHERE status IN ('confirmed', 'completed') AND client_id IS NOT NULL` | составной, частичный | визиты клиента уже по порядку для LAG |

Частичные индексы не содержат отменённых записей (8% строк) и записей без клиента — меньше и
быстрее. Миграция [`0007_analytics_indexes`](../src/beauty_agent/db/migrations/versions/0007_analytics_indexes.py)
строит их `CREATE INDEX CONCURRENTLY`: запись в `bookings` на базе пилотов не блокируется
(на 494 тыс. записей — 2,9 секунды на все три).

Отдельно переписан `master_load.sql`. `generate_series` по каждой записи (раскладка записи
по часовым клеткам) планировщик оценивал в 1 000 строк на запись (реально 1–2) и строил
параллельный план, где запуск воркеров дороже самой работы. Теперь число клеток считается
один раз в `MATERIALIZED` CTE, а смещения берутся из сетки с постоянными границами.

### Результат (салон с ~10 000 записей)

| Запрос | Период | До: время / буферы | После: время / буферы | План после |
| --- | --- | --- | --- | --- |
| загрузка мастеров по часам недели | 90 дней | 9,1 мс / 1 326 | 6,2 мс / 152 | Index Only Scan `ix_bookings_salon_period` |
| выручка и средний чек по неделям | год | 4,9 мс / 1 311 | 2,4 мс / 49 | Index Only Scan `ix_bookings_salon_period` |
| доля записей ИИ и вне рабочего времени | 30 дней | 2,7 мс / 1 311 | 0,18 мс / 6 | Index Only Scan `ix_bookings_salon_created` |
| возвращаемость клиентов (LAG) | 3 года | 11,8 мс / 3 077 | 7,6 мс / 1 830 | Index Only Scan `ix_bookings_salon_client_visits` |
| топ услуг с накопительным итогом | год | 3,9 мс / 1 328 | 1,7 мс / 66 | Index Only Scan `ix_bookings_salon_period` |

Время — `Execution Time` на сервере; с клиента добавляется ~1 мс на сеть и драйвер. Буферы — блоки
по 8 КБ, прочитанные из кэша: на холодной базе каждый из них — чтение с диска, поэтому разница
в буферах (в 10–200 раз) важнее разницы в миллисекундах на тёплом кэше.

### Коррелированный подзапрос против оконной функции

Возвращаемость можно написать «в лоб»: предыдущий визит — подзапросом `max(starts_at)` по тому же
клиенту для каждой записи. На тех же данных:

| Вариант | До индексов | После индексов |
| --- | --- | --- |
| коррелированный подзапрос | 87,8 мс, 77 694 буфера | 15,3 мс, 25 667 буферов |
| `lag() OVER (PARTITION BY client_id ORDER BY starts_at)` | 11,8 мс, 3 077 | 7,6 мс, 1 830 |

Подзапрос выполняется для каждой из ~8 500 записей; окно проходит записи один раз. Индекс помогает
обоим, но оконный вариант читает в 14 раз меньше блоков.

### Keyset-пагинация против OFFSET

Последняя страница (50 сообщений) диалога со 100 010 сообщениями:

| Вариант | Время | Буферы | План |
| --- | --- | --- | --- |
| `ORDER BY created_at DESC, id DESC LIMIT 50 OFFSET 99960` | 42,9 мс | 2 232 | Bitmap Heap Scan + сортировка всех сообщений |
| `WHERE (created_at, id) < (:t, :id) ORDER BY … LIMIT 50` | 0,07 мс | 7 | Index Scan Backward `ix_messages_conversation_id_created_at` |

OFFSET перебирает и выбрасывает все пропущенные строки — время растёт с номером страницы;
keyset начинает с нужного места индекса и читает ровно страницу. Поэтому API инбокса
(`/api/v1/admin/...`) отдаёт курсор, а не номер страницы.

### pg_stat_statements

Срез после прогона замеров (`shared_preload_libraries=pg_stat_statements`), по суммарному времени:

| Запрос | Вызовов | Среднее | Блоков всего |
| --- | --- | --- | --- |
| сообщения с OFFSET | 6 | 34,6 мс | 13 395 |
| возвращаемость, коррелированный подзапрос | 5 | 16,0 мс | 128 335 |
| возвращаемость, LAG | 5 | 5,7 мс | 9 135 |
| загрузка мастеров | 5 | 4,6 мс | 715 |
| выручка по неделям | 5 | 1,9 мс | 245 |
| топ услуг | 5 | 1,2 мс | 330 |
| доля ИИ | 5 | 0,13 мс | 30 |
| сообщения keyset | 5 | 0,04 мс | 35 |

### Как повторить

```bash
docker run -d --rm --name load-pg -e POSTGRES_USER=beauty -e POSTGRES_PASSWORD=beauty -e POSTGRES_DB=load \
  -p 127.0.0.1:47322:5432 pgvector/pgvector:pg17 -c shared_preload_libraries=pg_stat_statements
POSTGRES_PORT=47322 POSTGRES_DB=load poetry run alembic upgrade 0006
docker exec -i load-pg psql -U beauty -d load -v scale=0.1 -f - < scripts/generate_load_data.sql
POSTGRES_PORT=47322 POSTGRES_DB=load poetry run python -m scripts.explain_analytics --plans   # до
POSTGRES_PORT=47322 POSTGRES_DB=load poetry run alembic upgrade head
docker exec load-pg psql -U beauty -d load -c "VACUUM ANALYZE bookings"
POSTGRES_PORT=47322 POSTGRES_DB=load poetry run python -m scripts.explain_analytics --plans   # после
```

`VACUUM` после построения индексов обязателен для Index Only Scan: без карты видимости PostgreSQL
всё равно проверяет каждую строку в таблице.
