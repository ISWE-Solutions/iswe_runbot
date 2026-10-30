#!/usr/bin/env bash
# Regenerate tests/fixtures/odoo_logs: install each deliberately broken module from
# tests/fixtures/broken_modules on an Odoo version and keep its log. Needs the stack running.
#   scripts/capture_odoo_logs.sh 20.0
set -euo pipefail
cd "$(dirname "$0")/.."
version="${1:?usage: $0 <odoo version, e.g. 19.0>}"
password="$(grep '^ODOO_DB_PASSWORD=' .env | cut -d= -f2-)"
mods="$PWD/tests/fixtures/broken_modules"
out="$PWD/tests/fixtures/odoo_logs"
psql() { docker compose exec -T postgres psql -U "${POSTGRES_USER:-previewer}" -d "${POSTGRES_DB:-previewer}" -tAc "$1"; }
for dir in "$mods"/diag_*; do
  m="$(basename "$dir")"
  db="diag_${m#diag_}_${version%.0}"
  docker run --rm --network previewer_net --memory 1536m -v "$mods:/mnt/extra/0:ro" \
    --entrypoint odoo "odoo:${version%.0}" -d "$db" -i "$m" --stop-after-init \
    --addons-path=/mnt/extra/0 --db_host=postgres --db_user=odoo --db_password="$password" \
    --without-demo=all > "$out/$version-$m.log" 2>&1 || true
  psql "DROP DATABASE IF EXISTS \"$db\" WITH (FORCE)" > /dev/null
  echo "$version $m captured"
done
