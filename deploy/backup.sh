#!/usr/bin/env bash
# Ежесуточный бэкап PostgreSQL в отдельное хранилище с удалением старых копий.
# В бэкапе персональные данные клиентов (152-ФЗ): файл доступен только владельцу, шифруется
# публичным ключом GPG, а внешнее хранилище должно находиться в РФ.
#
# cron на сервере (от пользователя deploy):
#   30 3 * * * /opt/beauty-agent/backup.sh >> /var/log/beauty-agent/backup.log 2>&1
#
# Переменные (в /opt/beauty-agent/.env или окружении):
#   BACKUP_DIR            локальная папка, по умолчанию /var/backups/beauty-agent
#   BACKUP_KEEP_DAYS      сколько дней хранить копии, по умолчанию 14
#   BACKUP_GPG_RECIPIENT  ID или e-mail публичного ключа GPG; без него бэкап не создаётся
#   BACKUP_REMOTE         цель rclone, например s3-ru:beauty-backups; пусто — только локально
#
# Восстановление: deploy/README.md, раздел «Бэкапы».
set -euo pipefail
umask 077

cd "$(dirname "$0")"
COMPOSE=(docker compose -f docker-compose.prod.yml)

if [[ -f .env ]]; then
    # Только переменные бэкапа, а не весь .env: в нём пароли, которым нечего делать в окружении скрипта.
    # Без eval и source: значение из файла не выполняется как команда.
    while IFS='=' read -r name value; do
        if [[ "$name" =~ ^BACKUP_[A-Z_]+$ ]]; then
            export "$name=$value"
        fi
    done < <(grep -E '^BACKUP_[A-Z_]+=' .env || true)
fi

BACKUP_DIR="${BACKUP_DIR:-/var/backups/beauty-agent}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
: "${BACKUP_GPG_RECIPIENT:?BACKUP_GPG_RECIPIENT не задан: бэкап с ПДн не хранится незашифрованным}"

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$BACKUP_DIR/beauty-$stamp.dump.gpg"
partial="$target.partial"
trap 'rm -f "$partial"' EXIT

# Формат custom уже сжат и восстанавливается pg_restore выборочно; в открытом виде на диск не попадает
"${COMPOSE[@]}" exec -T postgres sh -c 'pg_dump --format=custom --no-owner -U "$POSTGRES_USER" "$POSTGRES_DB"' \
    | gpg --batch --yes --trust-model always --encrypt --recipient "$BACKUP_GPG_RECIPIENT" --output "$partial"

if [[ ! -s "$partial" ]]; then
    echo "$(date -u +%FT%TZ) ERROR: пустой бэкап" >&2
    exit 1
fi
mv "$partial" "$target"
echo "$(date -u +%FT%TZ) OK: $target ($(du -h "$target" | cut -f1))"

find "$BACKUP_DIR" -name 'beauty-*.dump.gpg' -type f -mtime +"$BACKUP_KEEP_DAYS" -delete

if [[ -n "${BACKUP_REMOTE:-}" ]]; then
    rclone copy "$target" "$BACKUP_REMOTE"
    rclone delete --min-age "${BACKUP_KEEP_DAYS}d" --include 'beauty-*.dump.gpg' "$BACKUP_REMOTE"
    echo "$(date -u +%FT%TZ) OK: скопирован в $BACKUP_REMOTE"
fi
