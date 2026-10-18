#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

docker compose -f compose.dev.yml build
docker compose -f compose.dev.yml run --rm --no-deps monix_dev python3 scripts/init-dev-users.py
docker compose -f compose.dev.yml up -d monix_dev

printf '%s\n' \
    "MONIX développement est disponible sur http://127.0.0.1:5481" \
    "Compte utilisateur : user / user" \
    "Compte administrateur : admin / admin"
