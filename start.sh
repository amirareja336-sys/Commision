#!/usr/bin/env bash
# start.sh — launch the Reconciliation Console and expose it on the LAN.
#
# Usage:
#   ./start.sh           # defaults: port 8000, auto-detects LAN IP
#   PORT=9000 ./start.sh # use a different port

set -e

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
STATIC_IP="${STATIC_IP:-192.168.1.2}"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"

# Activate venv if it exists next to this script
if [ -f "$APP_DIR/venv/bin/activate" ]; then
    source "$APP_DIR/venv/bin/activate"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Reconciliation Console"
echo "  Local:   http://localhost:${PORT}"
echo "  Network: http://${STATIC_IP}:${PORT}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

cd "$APP_DIR"
exec uvicorn backend.main:app \
    --host "$HOST" \
    --port "$PORT"
