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
printf 'OPENAI_API_KEY=%s\nOPENAI_MODEL=gpt-5.4-mini\nOPENAI_VISION_MODEL=gpt-5.4-mini\n' "$api_key" > "$temporary_file"
mv "$temporary_file" "$ENV_FILE"
unset api_key

echo "OpenAI API key saved securely to $ENV_FILE"
