#!/usr/bin/env bash
# start.sh — launch the Reconciliation Console and expose it on the LAN.
#
# Usage:
#   ./start.sh           # defaults: port 8000, auto-detects LAN IP
#   PORT=9000 ./start.sh # use a different port

set -e

PORT="${PORT:-8000}"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"

# Activate venv if it exists next to this script
if [ -f "$APP_DIR/venv/bin/activate" ]; then
    source "$APP_DIR/venv/bin/activate"
fi

# Print the LAN IP so you can share it with other PCs on the network
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Reconciliation Console"
echo "  Local:   http://localhost:${PORT}"
if [ -n "$LAN_IP" ]; then
    echo "  Network: http://${LAN_IP}:${PORT}  ← share this with other PCs"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

cd "$APP_DIR"
exec uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --reload
