#!/usr/bin/env bash
# Еженедельная проверка: последний бэкап действительно восстанавливается. Бэкап, который ни разу
# не восстанавливали, — не бэкап.
#
# Запускается на отдельной машине в РФ (небольшая VM или рабочий компьютер администратора), где
# лежит закрытый ключ GPG бэкапов: на сервере он не хранится. Последний бэкап скачивается из
# хранилища (BACKUP_REMOTE, rclone); на машине нужны docker, gpg, rclone. Рядом со скриптом — .env:
#   BACKUP_REMOTE=s3-ru:beauty-backups
#   RESTORE_CHECK_PING_URL=https://hc.example.ru/ping/<uuid>
# cron (воскресенье, после ночного бэкапа):
#   0 6 * * 0 /opt/restore-check/restore_check.sh >> /var/log/restore-check.log 2>&1
# Без BACKUP_REMOTE проверяются локальные копии из BACKUP_DIR.
#
# - Расшифровка идёт потоком прямо в pg_restore: открытые персональные данные не попадают на диск.
# - Одноразовая база — контейнер без опубликованных портов во внутренней сети Docker без выхода
#   наружу; после проверки удаляется вместе с сетью и скачанными копиями.
# - RESTORE_CHECK_PING_URL — мониторинг «мёртвой руки» (healthchecks и подобные): пинг после успеха.
#   Не прошла проверка или cron не запустился — пинга нет, и мониторинг сам присылает тревогу.
set -euo pipefail
umask 077

cd "$(dirname "$0")"
started=$SECONDS

if [[ -f .env ]]; then
    # Только свои переменные, без eval и source — как в backup.sh
    while IFS='=' read -r name value; do
        if [[ "$name" =~ ^(BACKUP|RESTORE_CHECK)_[A-Z_]+$ ]]; then
            export "$name=$value"
        fi
    done < <(grep -E '^(BACKUP|RESTORE_CHECK)_[A-Z_]+=' .env || true)
fi
BACKUP_DIR="${BACKUP_DIR:-/var/backups/beauty-agent}"
MAX_AGE_HOURS="${RESTORE_CHECK_MAX_AGE_HOURS:-36}"
download=""
name="restore-check-$(date +%s)"
password="$(openssl rand -hex 16)"

cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker network rm "$name" >/dev/null 2>&1 || true
    if [[ -n "$download" ]]; then rm -rf "$download"; fi
}
trap cleanup EXIT

fail() {
    echo "$(date -u +%FT%TZ) ERROR: $*" >&2
    exit 1
}

if [[ -n "${BACKUP_REMOTE:-}" ]]; then
    # копии последних двух суток — во временную папку, которая удаляется в конце
    download="$(mktemp -d)"
    rclone copy --max-age 48h --include 'beauty-*.dump.gpg' "$BACKUP_REMOTE" "$download"
    BACKUP_DIR="$download"
fi

# sed дочитывает вывод до конца: с head -1 sort получил бы SIGPIPE, и pipefail уронил бы скрипт
latest="$(find "$BACKUP_DIR" -name 'beauty-*.dump.gpg' -type f -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1s/^[^ ]* //p')"
[[ -n "$latest" ]] || fail "в $BACKUP_DIR нет бэкапов"
age_hours=$(( ($(date +%s) - $(stat -c %Y "$latest")) / 3600 ))
(( age_hours <= MAX_AGE_HOURS )) || fail "последний бэкап старше $MAX_AGE_HOURS ч ($age_hours ч): ночной бэкап не работает"

docker network create --internal "$name" >/dev/null
docker run -d --name "$name" --network "$name" -e POSTGRES_PASSWORD="$password" pgvector/pgvector:pg17 >/dev/null
for _ in $(seq 1 60); do
    docker exec "$name" pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1 && break
    sleep 1
done
docker exec "$name" pg_isready -U postgres -h 127.0.0.1 >/dev/null || fail "одноразовая база не запустилась"

gpg --batch --quiet --decrypt "$latest" \
    | docker exec -i "$name" pg_restore -U postgres -d postgres --no-owner --exit-on-error \
    || fail "pg_restore не смог восстановить $latest"

query() {
    docker exec "$name" psql -U postgres -d postgres -tAc "$1"
}
version="$(query 'SELECT version_num FROM alembic_version')"
salons="$(query 'SELECT count(*) FROM salons')"
bookings="$(query 'SELECT count(*) FROM bookings')"
messages="$(query 'SELECT count(*) FROM messages')"
[[ -n "$version" ]] || fail "в восстановленной базе нет версии схемы"
(( salons > 0 )) || fail "в восстановленной базе нет салонов"

echo "$(date -u +%FT%TZ) OK: $(basename "$latest") восстановлен за $(( SECONDS - started )) с:" \
    "схема $version, салонов $salons, записей $bookings, сообщений $messages"

if [[ -n "${RESTORE_CHECK_PING_URL:-}" ]]; then
    curl -fsS -m 10 --retry 3 "$RESTORE_CHECK_PING_URL" >/dev/null || echo "WARNING: не удалось отправить пинг" >&2
fi
