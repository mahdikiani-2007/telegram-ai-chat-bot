#!/bin/sh
# Runs inside the container as the single entrypoint process. Launches the
# Local Bot API Server in the background (only if TG_API_ID/TG_API_HASH are
# set), then runs bot.py in the foreground as PID 1's replacement (`exec`)
# so Railway's stop/restart signals reach it directly.
set -e

DATA_DIR="${DATA_DIR:-/data}"

if [ -n "$TG_API_ID" ] && [ -n "$TG_API_HASH" ]; then
    echo "[start.sh] TG_API_ID/TG_API_HASH set — launching Local Bot API Server..."
    mkdir -p "$DATA_DIR/botapi"
    telegram-bot-api \
        --api-id="$TG_API_ID" \
        --api-hash="$TG_API_HASH" \
        --local \
        --http-port=8081 \
        --dir="$DATA_DIR/botapi" \
        --log="$DATA_DIR/botapi/log.txt" &
    BOTAPI_PID=$!

    # Actually wait for the port to accept connections instead of a blind
    # fixed sleep — a fixed sleep is exactly what caused the intermittent
    # "Connection refused" spam bot.py hit on some deploys (startup time
    # varies, a static number can't reliably cover every case). Bounded at
    # 30s as a safety net; bot.py's own retry-on-crash loop still tolerates
    # it if the server is unusually slow to come up.
    echo "[start.sh] Waiting for Local Bot API Server to accept connections on :8081..."
    i=0
    until python3 -c "
import socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect(('localhost', 8081))
except OSError:
    sys.exit(1)
" 2>/dev/null; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then
            echo "[start.sh] WARNING: Local Bot API Server not ready after 30s — starting bot.py anyway."
            break
        fi
        sleep 1
    done
    echo "[start.sh] Local Bot API Server ready after ${i}s (pid $BOTAPI_PID)."
else
    echo "[start.sh] TG_API_ID/TG_API_HASH not set — using the normal cloud Bot API (50MB upload cap)."
fi

echo "[start.sh] Starting bot.py..."
exec python3 bot.py
