#!/bin/sh
set -e

python -m app.worker &
WORKER_PID=$!

uvicorn app.main:app --host 0.0.0.0 --port 10300 &
UVICORN_PID=$!

term() {
  kill -TERM "$WORKER_PID" "$UVICORN_PID" 2>/dev/null || true
}

trap term TERM INT

while :; do
  if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    wait "$WORKER_PID" || true
    term
    wait "$UVICORN_PID" || true
    exit 1
  fi
  if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
    wait "$UVICORN_PID" || true
    term
    wait "$WORKER_PID" || true
    exit 1
  fi
  sleep 2
done
