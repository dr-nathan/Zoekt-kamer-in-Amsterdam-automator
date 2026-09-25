#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/home/nathan/facebookrooms
ENV_FILE="$APP_DIR/.env"
ADMIN_HASH_FILE=/home/nathan/.facebookrooms-admin-password-hash
SITE_HASH_FILE=/home/nathan/.facebookrooms-password-hash

if [[ "$(id -un)" != "nathan" ]]; then
    echo "Run this script as nathan, not root." >&2
    exit 1
fi

if [[ ! -s "$SITE_HASH_FILE" ]]; then
    echo "Missing existing website password hash: $SITE_HASH_FILE" >&2
    exit 1
fi

read -r -s -p "New password for the separate /admin login: " admin_password
printf '\n'
if [[ ${#admin_password} -lt 14 ]]; then
    echo "Use at least 14 characters for the admin password." >&2
    exit 1
fi
admin_hash=$(caddy hash-password --plaintext "$admin_password")
unset admin_password
umask 077
printf '%s\n' "$admin_hash" > "$ADMIN_HASH_FILE"
unset admin_hash

read -r -s -p "Resend API key (leave empty to configure later): " resend_api_key
printf '\n'
read -r -p "Verified sender [Chineur2000 <alerts@facebookrooms.nl>]: " resend_from
if [[ -z "$resend_from" ]]; then
    resend_from='Chineur2000 <alerts@facebookrooms.nl>'
fi
read -r -s -p "Resend webhook signing secret (leave empty for later): " resend_webhook_secret
printf '\n'
read -r -s -p "Telegram bot token (leave empty to configure later): " telegram_bot_token
printf '\n'
read -r -p "Telegram bot username without @ (leave empty for later): " telegram_bot_username

app_secret=$(openssl rand -hex 32)
telegram_webhook_secret=$(openssl rand -hex 24)

export ENV_FILE app_secret resend_api_key resend_from resend_webhook_secret
export telegram_bot_token telegram_bot_username telegram_webhook_secret
python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["ENV_FILE"])
existing = {}
if path.exists():
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        existing[key] = value

updates = {
    "PUBLIC_BASE_URL": "https://facebookrooms.nl",
    "APP_SECRET": existing.get("APP_SECRET") or os.environ["app_secret"],
    "RESEND_API_KEY": os.environ["resend_api_key"],
    "RESEND_FROM": os.environ["resend_from"],
    "RESEND_WEBHOOK_SECRET": os.environ["resend_webhook_secret"],
    "TELEGRAM_BOT_TOKEN": os.environ["telegram_bot_token"],
    "TELEGRAM_BOT_USERNAME": os.environ["telegram_bot_username"],
    "TELEGRAM_WEBHOOK_SECRET": (
        existing.get("TELEGRAM_WEBHOOK_SECRET")
        or os.environ["telegram_webhook_secret"]
    ),
}
for key, value in updates.items():
    if value or key in {"PUBLIC_BASE_URL", "APP_SECRET", "TELEGRAM_WEBHOOK_SECRET"}:
        existing[key] = value

path.write_text("".join(f"{key}={value}\n" for key, value in existing.items()))
path.chmod(0o600)
PY

unset app_secret resend_api_key resend_from resend_webhook_secret
telegram_webhook_secret=$(sed -n 's/^TELEGRAM_WEBHOOK_SECRET=//p' "$ENV_FILE")

site_hash=$(sed -n '1p' "$SITE_HASH_FILE")
admin_hash=$(sed -n '1p' "$ADMIN_HASH_FILE")
temporary_caddy=$(mktemp)
sed -e "s|REPLACE_WITH_CADDY_PASSWORD_HASH|$site_hash|" \
    -e "s|REPLACE_WITH_ADMIN_PASSWORD_HASH|$admin_hash|" \
    "$APP_DIR/deploy/Caddyfile.example" > "$temporary_caddy"

sudo install -m 0644 "$temporary_caddy" /etc/caddy/Caddyfile
rm -f "$temporary_caddy"
sudo caddy fmt --overwrite /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile

sudo install -m 0644 "$APP_DIR/deploy/facebookrooms-digest.service" \
    /etc/systemd/system/facebookrooms-digest.service
sudo install -m 0644 "$APP_DIR/deploy/facebookrooms-digest.timer" \
    /etc/systemd/system/facebookrooms-digest.timer
sudo systemctl daemon-reload
sudo systemctl enable --now facebookrooms-digest.timer
sudo systemctl restart facebookrooms.service
sudo systemctl reload caddy

if [[ -n "$telegram_bot_token" && -n "$telegram_bot_username" ]]; then
    response=$(curl -fsS \
        -H 'Content-Type: application/json' \
        -d "{\"url\":\"https://facebookrooms.nl/webhooks/telegram\",\"secret_token\":\"$telegram_webhook_secret\",\"allowed_updates\":[\"message\",\"callback_query\"]}" \
        "https://api.telegram.org/bot${telegram_bot_token}/setWebhook")
    printf 'Telegram webhook: %s\n' "$response"
fi

unset telegram_bot_token telegram_bot_username telegram_webhook_secret
echo "Notification services configured."
systemctl list-timers --all facebookrooms-digest.timer --no-pager
