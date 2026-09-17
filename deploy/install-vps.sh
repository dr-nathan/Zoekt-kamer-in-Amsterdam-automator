#!/bin/sh
set -eu

APP_DIR=/home/nathan/facebookrooms
DATA_DIR=/home/nathan/facebookrooms-data
PASSWORD_HASH_FILE=/home/nathan/.facebookrooms-password-hash

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

if [ ! -s "$PASSWORD_HASH_FILE" ]; then
    echo "Missing website password hash: $PASSWORD_HASH_FILE" >&2
    exit 1
fi

apt-get update
apt-get install -y python3-venv

install -d -o nathan -g nathan -m 0750 "$DATA_DIR"
python3 -m venv --clear "$APP_DIR/.venv"
chown -R nathan:nathan "$APP_DIR/.venv"

runuser -u nathan -- "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
runuser -u nathan -- "$APP_DIR/.venv/bin/python" -m pip install -e "$APP_DIR"
"$APP_DIR/.venv/bin/python" -m playwright install-deps chromium
runuser -u nathan -- env HOME=/home/nathan \
    "$APP_DIR/.venv/bin/python" -m playwright install chromium

install -m 0644 "$APP_DIR/deploy/facebookrooms.service" \
    /etc/systemd/system/facebookrooms.service
install -m 0644 "$APP_DIR/deploy/facebookrooms-collect.service" \
    /etc/systemd/system/facebookrooms-collect.service
install -m 0644 "$APP_DIR/deploy/facebookrooms-collect.timer" \
    /etc/systemd/system/facebookrooms-collect.timer

password_hash=$(sed -n '1p' "$PASSWORD_HASH_FILE")
sed "s|REPLACE_WITH_CADDY_PASSWORD_HASH|$password_hash|" \
    "$APP_DIR/deploy/Caddyfile.example" > /etc/caddy/Caddyfile
caddy fmt --overwrite /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile

systemctl daemon-reload
systemctl enable --now facebookrooms.service
systemctl reload caddy

echo
echo "Website service: $(systemctl is-active facebookrooms.service)"
echo "Caddy service:   $(systemctl is-active caddy.service)"
echo "The collection timer remains disabled until the Facebook session and API key are ready."
