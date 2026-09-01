#!/usr/bin/env bash
# start.sh — launch the Reconciliation Console and expose it on the LAN.
#
# Usage:
#   ./start.sh           # defaults: port 8000, auto-detects LAN IP
#   PORT=9000 ./start.sh # use a different port
#   SCENARIO=1.2 ./start.sh  # prefer/create that test scenario DB

set -e

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
STATIC_IP="${STATIC_IP:-192.168.1.2}"
SCENARIO="${SCENARIO:-1.1}"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv "$APP_DIR/venv" 2>/dev/null || python -m venv "$APP_DIR/venv"
fi
# Activate venv if it exists next to this script
if [ -f "$APP_DIR/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$APP_DIR/venv/bin/activate"
fi

if [ -f "$APP_DIR/requirements.txt" ]; then
    pip install -q -r "$APP_DIR/requirements.txt" || true
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Reconciliation Console"
echo "  Local:   http://localhost:${PORT}"
echo "  Network: http://${STATIC_IP}:${PORT}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

cd "$APP_DIR"

# Dev/test mode: operate only on the generated test DB under data/test_<scenario>/test.db
SCENARIO_DIR="${SCENARIO//./_}"
TEST_DB_PATH="$APP_DIR/data/test_${SCENARIO_DIR}/test.db"

if [ -n "${COMMISSIONS_DB:-}" ]; then
    echo "Using COMMISSIONS_DB=$COMMISSIONS_DB"
elif [ -f "$TEST_DB_PATH" ]; then
    export COMMISSIONS_DB="$TEST_DB_PATH"
    echo "Found test DB at $COMMISSIONS_DB — starting in test/dev mode."
else
    echo "No test DB found at $TEST_DB_PATH."
    read -r -p "Create a development test DB and dev user for scenario ${SCENARIO}? [Y/n] " yn
    yn="${yn:-Y}"
    if [[ "$yn" =~ ^[Yy]$ ]]; then
        echo "Creating test DB (scenario $SCENARIO)..."
        python "$APP_DIR/tests/test file generator/generate_test_data.py" --scenario "$SCENARIO"
        export COMMISSIONS_DB="$TEST_DB_PATH"
        echo "Created $COMMISSIONS_DB"
        echo "Log in as user \"dev\" with the password printed above, then open /testmode."
    else
        echo "Proceeding with default db/commissions.db."
    fi
fi

# Launch uvicorn (keep in background so we can open a browser), then wait.
UVICORN_BIN="$APP_DIR/venv/bin/uvicorn"
if [ ! -x "$UVICORN_BIN" ]; then
    UVICORN_BIN="uvicorn"
fi
"$UVICORN_BIN" backend.main:app --host "$HOST" --port "$PORT" &
UVICORN_PID=$!
sleep 1
LOCAL_URL="http://localhost:${PORT}"
echo "Server launched (pid $UVICORN_PID)."
if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$LOCAL_URL" || true
fi
echo "Open the app at: $LOCAL_URL"
wait "$UVICORN_PID"
