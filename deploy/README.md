# Деплой на сервер

Один VPS в РФ с Docker: api, PostgreSQL, Redis и Caddy (HTTPS). Langfuse — отдельно (блок 5),
ему нужно ещё 4+ ГБ ОЗУ.

## 1. Сервер

- VPS в РФ: 2 vCPU, 2–4 ГБ ОЗУ, 40 ГБ диска, Ubuntu 24.04. Данные клиентов не покидают РФ (152-ФЗ).
- DNS: A-запись домена (например, `app.example.ru`) на IP сервера.
- Пользователь для деплоя без root, firewall и вход только по ключу:

```bash
adduser --disabled-password deploy && usermod -aG docker deploy
mkdir -p /opt/beauty-agent && chown deploy: /opt/beauty-agent
# положить публичный ключ в /home/deploy/.ssh/authorized_keys, затем:
sudo DEPLOY_USER=deploy ./firewall.sh   # ufw: только SSH (с лимитом попыток), 80, 443; sshd — только ключ
```

Порты PostgreSQL, Redis, RabbitMQ, Prometheus наружу не открываются: compose их не публикует,
Grafana слушает только 127.0.0.1. Docker публикует порты мимо ufw — поэтому важно, что
опубликованы только 80 и 443 у Caddy.

**Таймауты на каждом звене**: Caddy — заголовки запроса 10 с, тело 30 с, простой соединения 2 мин,
ответ api — 60 с; uvicorn — keep-alive 5 с (меньше, чем у Caddy), 25 с на завершение при остановке;
PostgreSQL — `statement_timeout` 15 с и `idle_in_transaction_session_timeout` 60 с для приложения
(у миграций без ограничения), ожидание соединения из пула — 10 с; Redis — 2 с; HTTP-клиенты
YCLIENTS, LLM и мессенджеров — 10–30 с; задачи Celery — 300 с.

## 2. Секреты на сервере

`/opt/beauty-agent/.env` (права 600, владелец deploy) — по образцу `.env.example`:

```dotenv
APP_IMAGE=ghcr.io/<owner>/ai-beauty-agent:<sha>
DOMAIN=app.example.ru
ACME_EMAIL=you@example.ru
POSTGRES_USER=beauty
POSTGRES_PASSWORD=<openssl rand -hex 32>
POSTGRES_DB=beauty
REDIS_PASSWORD=<openssl rand -hex 32>
YCLIENTS_PARTNER_TOKEN=...
SENTRY_DSN=...
SENTRY_ENVIRONMENT=prod
BACKUP_GPG_RECIPIENT=backup@example.ru
BACKUP_REMOTE=s3-ru:beauty-backups
```

Строка `APP_IMAGE=` должна быть: деплой обновляет её на версию, которая сейчас работает.

Если пакет в GHCR приватный, на сервере один раз: `docker login ghcr.io` с токеном
(classic PAT только с правом `read:packages`).

## 3. Автодеплой из GitHub

1. Settings → Environments → `production`: секреты `DEPLOY_HOST`, `DEPLOY_USER`,
   `DEPLOY_SSH_KEY` (отдельный ключ ed25519 только для деплоя) и `DEPLOY_KNOWN_HOSTS`
   (вывод `ssh-keyscan -t ed25519 <host>`, проверенный по отпечатку из консоли хостера).
2. Settings → Variables: `DEPLOY_ENABLED=true`.
3. Каждый зелёный CI на `main` собирает образ, отправляет его в GHCR, копирует на сервер
   compose-файлы, применяет миграции, перезапускает контейнеры и ждёт `/ready`.
4. **Автооткат**: если новая версия не ответила на `/ready` за минуту, деплой поднимает прошлый
   образ (из `.env`) и падает с ошибкой. Схема при этом не откатывается: миграции только
   обратно совместимые — `scripts/check_migrations.py` в CI не пропустит удаление или
   переименование колонки и `NOT NULL` без значения по умолчанию. Ломающее изменение делается
   в два релиза (expand/contract) и помечается в миграции комментарием `# contract: <причина>`.

Ручной запуск — Actions → Deploy → Run workflow.

## 4. Бэкапы

`backup.sh` раз в сутки делает `pg_dump`, шифрует его публичным ключом GPG и копирует
в хранилище в РФ (rclone, например Yandex Object Storage), удаляя копии старше 14 дней.

```bash
gpg --import backup-public.asc          # приватный ключ на сервере не хранится
crontab -e   # 30 3 * * * /opt/beauty-agent/backup.sh >> /var/log/beauty-agent/backup.log 2>&1
```

**Проверка восстановления — каждую неделю, автоматически.** `restore_check.sh` на отдельной
машине в РФ, где лежит приватный ключ: скачивает последний бэкап из хранилища, потоком расшифровывает
его в одноразовый PostgreSQL без портов наружу, проверяет версию схемы и данные и пингует мониторинг.
Нет пинга — тревога: бэкап не восстанавливается, не делается или проверка не запустилась.

```bash
# на машине проверки: docker, gpg (с приватным ключом), rclone с доступом к хранилищу
mkdir -p /opt/restore-check && cp restore_check.sh /opt/restore-check/
printf 'BACKUP_REMOTE=s3-ru:beauty-backups
RESTORE_CHECK_PING_URL=https://hc.example.ru/ping/<uuid>
'   > /opt/restore-check/.env && chmod 600 /opt/restore-check/.env
crontab -e   # 0 6 * * 0 /opt/restore-check/restore_check.sh >> /var/log/restore-check.log 2>&1
```

## 5. Мониторинг

- Ошибки — Sentry или self-hosted GlitchTip (`SENTRY_DSN`); ПДн вычищаются до отправки (`core/sentry.py`).
- Внешняя проверка `https://<домен>/health` раз в минуту — UptimeRobot или Healthchecks.io
  с уведомлением в Telegram: падение сервера видно, даже когда сам сервер не может ничего отправить.
- Логи: `docker compose -f docker-compose.prod.yml logs -f api` (JSON, телефоны замаскированы,
  ротация 5 × 10 МБ на контейнер).
