#!/usr/bin/env bash
# Local dev entrypoint: (re)starts a cloudflared quick tunnel, points .env's
# TELEGRAM_WEBHOOK_BASE_URL at its fresh URL, then runs the app. Meant to be wired up
# as a single PyCharm "Shell Script" run configuration - Start runs this whole flow,
# Stop kills it (and the tunnel, via the trap below).
#
# Deliberately runs uvicorn WITHOUT --reload: its two-process reloader/worker split
# can leave the reloader alive (and the port held) even when the worker's lifespan
# startup fails, which defeats a clean Stop. Use the plain Start/Debug config instead
# for fast code-reload iteration; this one is for reliably testing the real webhook.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT=8000
ENV_FILE=.env
LOG_FILE="$(mktemp -t cloudflared_log)"

if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE - copy .env.example to .env and fill it in first." >&2
    exit 1
fi

free_port() {
    local port=$1
    for attempt in 1 2 3; do
        local pids
        pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
        if [ -z "$pids" ]; then
            return 0
        fi
        echo "Freeing port $port (attempt $attempt, pid(s): $pids)..."
        if [ "$attempt" -ge 3 ]; then
            kill -9 $pids 2>/dev/null || true
        else
            kill $pids 2>/dev/null || true
        fi
        sleep 2
    done
}

echo "Stopping any existing tunnel for this app..."
pkill -f "cloudflared tunnel --url http://localhost:$PORT" 2>/dev/null || true
sleep 1

echo "Making sure port $PORT is free (e.g. a leftover process from a previous run)..."
free_port "$PORT"

TUNNEL_PID=""
APP_PID=""

# Bash does NOT forward signals to a foreground child by default, so running uvicorn
# as the script's last (exec'd) command would mean Stop/SIGTERM never reaches this
# trap. Both children run in the background instead, and `wait` at the very end - a
# builtin, not an external process - is what actually gets interrupted by an incoming
# signal, so the trap fires promptly instead of only after uvicorn exits on its own.
cleanup() {
    echo "Shutting down..."
    # if-blocks, not `[ -n "$x" ] && kill ...`: under `set -e`, a false test in that
    # `&&` form is itself a non-zero status and would abort this function early,
    # skipping the second kill entirely.
    if [ -n "$APP_PID" ]; then
        kill "$APP_PID" 2>/dev/null || true
    fi
    if [ -n "$TUNNEL_PID" ]; then
        kill "$TUNNEL_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "Starting a new cloudflared tunnel..."
cloudflared tunnel --url "http://localhost:$PORT" > "$LOG_FILE" 2>&1 &
TUNNEL_PID=$!

echo "Waiting for tunnel URL..."
URL=""
for _ in $(seq 1 30); do
    if grep -qE "failed to request quick Tunnel" "$LOG_FILE" 2>/dev/null; then
        echo "cloudflared failed to create a tunnel - see $LOG_FILE:" >&2
        cat "$LOG_FILE" >&2
        exit 1
    fi
    # Real quick-tunnel hostnames are always multi-word-hyphenated (e.g.
    # does-provinces-killing-customs.trycloudflare.com). Requiring a hyphen also
    # rules out matching cloudflared's OWN api.trycloudflare.com endpoint, which
    # shows up verbatim in its failure output and would otherwise look like a hit.
    URL=$(grep -Eo 'https://[a-zA-Z0-9]+(-[a-zA-Z0-9]+)+\.trycloudflare\.com' "$LOG_FILE" | head -1 || true)
    if [ -n "$URL" ]; then
        break
    fi
    sleep 1
done

if [ -z "$URL" ]; then
    echo "Failed to get a tunnel URL after 30s - see $LOG_FILE" >&2
    cat "$LOG_FILE" >&2
    exit 1
fi

echo "Tunnel URL: $URL"

# cloudflared prints the URL as soon as it's assigned, but the hostname can take a
# few more seconds to actually become resolvable on Cloudflare's edge - registering
# the Telegram webhook before that fails with "Failed to resolve host". Query a
# public resolver directly (not the local one) so a cached NXDOMAIN doesn't stall this.
HOST=${URL#https://}
echo "Waiting for $HOST to resolve..."
RESOLVED=""
for _ in $(seq 1 20); do
    RESOLVED=$(dig +short "$HOST" @1.1.1.1 2>/dev/null || true)
    if [ -n "$RESOLVED" ]; then
        break
    fi
    sleep 1
done
if [ -z "$RESOLVED" ]; then
    echo "Warning: $HOST still not resolving after 20s, continuing anyway..." >&2
fi

sed -i '' "s#^TELEGRAM_WEBHOOK_BASE_URL=.*#TELEGRAM_WEBHOOK_BASE_URL=$URL#" "$ENV_FILE"

echo "Starting app..."
uv run uvicorn app.main:app --port "$PORT" &
APP_PID=$!
wait "$APP_PID"
