#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/home/nathan/facebookrooms
ENV_FILE="$APP_DIR/.env"

read -r -s -p "OpenAI API key: " api_key
printf '\n'

if [[ "$api_key" != sk-* ]]; then
    echo "That does not look like an OpenAI API key; nothing was changed." >&2
    exit 1
fi

umask 077
temporary_file=$(mktemp "$APP_DIR/.env.XXXXXX")
trap 'rm -f "$temporary_file"' EXIT

if [[ -f "$ENV_FILE" ]]; then
    awk '!/^OPENAI_API_KEY=/ && !/^OPENAI_MODEL=/ && !/^OPENAI_VISION_MODEL=/' \
        "$ENV_FILE" > "$temporary_file"
fi
printf 'OPENAI_API_KEY=%s\nOPENAI_MODEL=gpt-5.4-mini\nOPENAI_VISION_MODEL=gpt-5.4-mini\n' \
    "$api_key" >> "$temporary_file"
chmod 600 "$temporary_file"
mv "$temporary_file" "$ENV_FILE"
trap - EXIT
unset api_key

echo "OpenAI API key updated securely in $ENV_FILE; the other settings were preserved."
