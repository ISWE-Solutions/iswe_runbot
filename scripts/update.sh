#!/usr/bin/env bash
# Update a server install to the latest code on GitHub, rebuild and restart.
#
#   sudo /opt/previewer/scripts/update.sh            # latest commit of the odoo branch
#   sudo /opt/previewer/scripts/update.sh <ref>      # another branch, a tag or a commit (rollback)
#   ... --force                                      # even when up to date or a build is running
#
# Keeps .env and everything under DATA_DIR. The commit that is running is in REVISION.
# A private repo needs GITHUB_TOKEN (read access) in the environment or in .env.
set -euo pipefail
{
[ "$(id -u)" = 0 ] || exec sudo -E "$0" "$@"

REPO="${PREVIEWER_REPO:-ISWE-Solutions/iswe_runbot}"
REF=odoo
FORCE=false
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) REF="$arg" ;;
  esac
done

cd "$(dirname "$(readlink -f "$0")")/.."
ROOT="$PWD"
[ -f .env ] || { echo "No .env in $ROOT: run scripts/install.sh first"; exit 1; }
set -a; . ./.env; set +a
exec > >(tee -a "$ROOT/update.log") 2>&1
echo "=== $(date '+%F %T') update to $REPO@$REF"

AUTH=()
[ -n "${GITHUB_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $GITHUB_TOKEN")
api() { curl -fsSL "${AUTH[@]}" -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$REPO/$1"; }

SHA=$(api "commits/$REF" | jq -r '.sha // empty') || true
[ -n "$SHA" ] || { echo "Cannot read $REF from GitHub (no such branch/commit, or a private repo: set GITHUB_TOKEN)"; exit 1; }
CURRENT=$(cat REVISION 2>/dev/null || echo none)
echo "Running: $CURRENT   Target: ${SHA:0:7} ($(api "commits/$SHA" | jq -r '.commit.message | split("\n")[0]'))"
if [ "${CURRENT:0:7}" = "${SHA:0:7}" ] && [ "$FORCE" = false ]; then
  echo "Already up to date."
  exit 0
fi

COMPOSE=(docker compose -f docker-compose.yml)
[ "${TLS_ENABLED:-false}" = "true" ] && COMPOSE+=(-f docker-compose.prod.yml)

BUILDING=$("${COMPOSE[@]}" exec -T postgres psql -U "${POSTGRES_USER:-previewer}" -d "${POSTGRES_DB:-previewer}" \
  -tAc "select count(*) from builds where status = 'building'" 2>/dev/null || echo 0)
if [ "${BUILDING:-0}" != 0 ] && [ "$FORCE" = false ]; then
  echo "$BUILDING build(s) running; restarting the worker would fail them. Wait, or rerun with --force."
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "${AUTH[@]}" "https://api.github.com/repos/$REPO/tarball/$SHA" | tar -xz -C "$TMP" --strip-components=1
KEEP=(--exclude /.env --exclude /REVISION --exclude '/*.log' --exclude /data/)
DATA=$(readlink -m "${DATA_DIR:-$ROOT/data}")
case "$DATA/" in "$ROOT"/*) KEEP+=(--exclude "/${DATA#"$ROOT"/}/") ;; esac  # never delete live data
rsync -a --delete "${KEEP[@]}" "$TMP/" "$ROOT/"
rm -f docker-compose.override.yml  # the dev override must never load on a server

"${COMPOSE[@]}" build
"${COMPOSE[@]}" up -d  # the app runs database migrations on start

echo "Waiting for the app to become healthy..."
for _ in $(seq 90); do
  STATE=$(docker inspect -f '{{.State.Health.Status}}' "$("${COMPOSE[@]}" ps -q app)" 2>/dev/null || true)
  [ "$STATE" = healthy ] && break
  sleep 2
done
"${COMPOSE[@]}" ps --format '{{.Service}}\t{{.Status}}'
if [ "$STATE" != healthy ]; then
  echo "The app is not healthy ($STATE). Logs: ${COMPOSE[*]} logs app --tail 100"
  echo "Roll back with: $0 $CURRENT"
  exit 1
fi
echo "$SHA" > REVISION
echo "Updated ${CURRENT:0:7} -> ${SHA:0:7}. Roll back with: $0 $CURRENT"
exit 0
}
