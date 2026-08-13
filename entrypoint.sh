#!/bin/sh
# Starts the web app (web.py) with uvicorn, bound to the port Railway
# injects via $PORT (falls back to 8000 for local runs, e.g.
# `docker run -p 8000:8000 ...`).
set -e

PORT="${PORT:-8000}"
echo "entrypoint: starting web app on 0.0.0.0:${PORT}"

exec uvicorn web:app --host 0.0.0.0 --port "$PORT"
