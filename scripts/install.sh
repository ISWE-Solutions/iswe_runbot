#!/usr/bin/env bash
# One-time server install (Ubuntu 24.04). Run from the repo root as a sudo-capable user.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v docker >/dev/null || { echo "Docker is not installed: https://docs.docker.com/engine/install/ubuntu/"; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose v2 plugin is missing"; exit 1; }
[ -f .env ] || { echo "Create .env first: cp .env.example .env and fill it in"; exit 1; }

set -a; . ./.env; set +a
: "${DATA_DIR:?DATA_DIR missing in .env}" "${ENCRYPTION_KEY:?}" "${SECRET_KEY:?}" "${POSTGRES_PASSWORD:?}" "${ADMIN_EMAIL:?}"

sudo mkdir -p "$DATA_DIR"/{repos,builds,filestore,logs/traefik,checkouts,letsencrypt}
sudo chown -R "$(id -u)":"$(id -g)" "$DATA_DIR"

COMPOSE=(docker compose -f docker-compose.yml)
[ "${TLS_ENABLED:-false}" = "true" ] && COMPOSE+=(-f docker-compose.prod.yml)

"${COMPOSE[@]}" build
"${COMPOSE[@]}" up -d   # the app runs migrations and seeds Odoo versions on start

echo "Waiting for the app to become healthy..."
for _ in $(seq 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' previewer-app-1 2>/dev/null)" = healthy ] && break
  sleep 2
done

echo "Create the first admin ($ADMIN_EMAIL):"
"${COMPOSE[@]}" exec app python -m previewer.cli create-user --email "$ADMIN_EMAIL" --name Admin
SCHEME=http; [ "${TLS_ENABLED:-false}" = "true" ] && SCHEME=https
echo "Done. Open $SCHEME://${DOMAIN}"
