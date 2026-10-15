# Деплой на сервер

Один VPS в РФ с Docker: api, PostgreSQL, Redis и Caddy (HTTPS). Langfuse — отдельно (блок 5),
ему нужно ещё 4+ ГБ ОЗУ.

## 1. Сервер

- VPS в РФ: 2 vCPU, 2–4 ГБ ОЗУ, 40 ГБ диска, Ubuntu 24.04. Данные клиентов не покидают РФ (152-ФЗ).
- DNS: A-запись домена (например, `app.example.ru`) на IP сервера.
- Пользователь для деплоя без root и вход только по ключу:

```bash
adduser --disabled-password deploy && usermod -aG docker deploy
mkdir -p /opt/beauty-agent && chown deploy: /opt/beauty-agent
# /etc/ssh/sshd_config: PasswordAuthentication no, PermitRootLogin no
ufw allow OpenSSH && ufw allow 80,443/tcp && ufw allow 443/udp && ufw enable
```

Порты PostgreSQL и Redis наружу не открываются: compose их не публикует.

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

Ручной запуск — Actions → Deploy → Run workflow.

## 4. Бэкапы

`backup.sh` раз в сутки делает `pg_dump`, шифрует его публичным ключом GPG и копирует
в хранилище в РФ (rclone, например Yandex Object Storage), удаляя копии старше 14 дней.

```bash
gpg --import backup-public.asc          # приватный ключ на сервере не хранится
crontab -e   # 30 3 * * * /opt/beauty-agent/backup.sh >> /var/log/beauty-agent/backup.log 2>&1
```

**Проверка восстановления** (один раз после настройки и после крупных миграций),
на отдельной машине, где есть приватный ключ:

```bash
gpg --decrypt beauty-<stamp>.dump.gpg > restore.dump
docker run -d --name restore-check -e POSTGRES_PASSWORD=check pgvector/pgvector:pg17
docker cp restore.dump restore-check:/tmp/
docker exec restore-check pg_restore -U postgres -d postgres --no-owner /tmp/restore.dump
docker exec restore-check psql -U postgres -c "select count(*) from bookings"
docker rm -f restore-check && shred -u restore.dump
```

## 5. Мониторинг

- Ошибки — Sentry или self-hosted GlitchTip (`SENTRY_DSN`); ПДн вычищаются до отправки (`core/sentry.py`).
- Внешняя проверка `https://<домен>/health` раз в минуту — UptimeRobot или Healthchecks.io
  с уведомлением в Telegram: падение сервера видно, даже когда сам сервер не может ничего отправить.
- Логи: `docker compose -f docker-compose.prod.yml logs -f api` (JSON, телефоны замаскированы,
  ротация 5 × 10 МБ на контейнер).
