#!/usr/bin/env bash

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUNICORN="$APP_ROOT/venv/bin/gunicorn"

if [[ ! -x "$GUNICORN" ]]; then
    echo "error: $GUNICORN not found." >&2
    echo "set up the venv first:  cd '$APP_ROOT' && python3 -m venv venv && venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi


threads="${GUNICORN_THREADS:-32}"

cd "$APP_ROOT"
exec "$GUNICORN" -w 1 --threads "$threads" -b "0.0.0.0:${PORT:-5000}" server:app
