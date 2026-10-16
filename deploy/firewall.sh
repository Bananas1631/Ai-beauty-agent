#!/usr/bin/env bash
# Однократная настройка сервера: снаружи открыты только SSH по ключу, 80 и 443.
#
#   sudo DEPLOY_USER=deploy ./firewall.sh
#
# - ufw: входящие запрещены, кроме SSH (с ограничением частоты попыток), HTTP и HTTPS (и UDP 443 для HTTP/3).
# - SSH: только по ключу, без входа root и паролей. Перед этим скрипт проверяет, что у DEPLOY_USER
#   есть ключ, — иначе можно запереть себя снаружи.
# - Docker публикует порты мимо ufw (правила iptables). Поэтому в docker-compose.prod.yml наружу
#   опубликованы только 80 и 443 у Caddy, Grafana — на 127.0.0.1, остальное — только внутри сети Docker.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Запускать от root (sudo)" >&2; exit 1; }
: "${DEPLOY_USER:?Укажите DEPLOY_USER — пользователя, под которым деплой заходит по ключу}"
SSH_PORT="${SSH_PORT:-22}"

keys="/home/$DEPLOY_USER/.ssh/authorized_keys"
[[ -s "$keys" ]] || { echo "У $DEPLOY_USER нет ключа в $keys — вход по паролю ещё нужен" >&2; exit 1; }

command -v ufw >/dev/null || apt-get install -y ufw

ufw default deny incoming
ufw default allow outgoing
ufw limit "$SSH_PORT/tcp" comment 'SSH: не больше 6 попыток за 30 секунд с адреса'
ufw allow 80/tcp comment 'HTTP: Caddy, выпуск сертификата и редирект на HTTPS'
ufw allow 443/tcp comment 'HTTPS'
ufw allow 443/udp comment 'HTTP/3'
ufw --force enable  # текущее SSH-соединение не рвётся

cat > /etc/ssh/sshd_config.d/10-beauty-agent.conf <<EOF
# beauty-agent: вход только по ключу
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
MaxAuthTries 3
LoginGraceTime 20
AllowUsers $DEPLOY_USER
EOF
sshd -t  # ошибка в конфигурации — выход до перезапуска, доступ не теряется
systemctl reload ssh 2>/dev/null || systemctl reload sshd

ufw status verbose
echo "Готово. Проверьте вход по ключу в НОВОМ окне терминала, прежде чем закрывать это."
