#!/bin/sh
# Container entrypoint for the `app` service: migrate, seed, serve.
set -e
alembic upgrade head
python -m previewer.cli seed
exec uvicorn previewer.web.main:app --host 0.0.0.0 --port 8000 \
    --proxy-headers --forwarded-allow-ips='*' ${UVICORN_EXTRA}
