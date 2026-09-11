#!/usr/bin/env bash
# Local dev entrypoint: (re)starts a cloudflared quick tunnel, points .env's
# TELEGRAM_WEBHOOK_BASE_URL at its fresh URL, then runs the app. Meant to be wired up
# as a single PyCharm "Shell Script" run configuration - Start runs this whole flow,
# Stop kills it (and the tunnel, via the trap below).
#
# No breakpoints here: PyCharm's Python debugger can't attach through a shell script
# that launches uvicorn as a subprocess. To debug with a real webhook, use
# start_tunnel.sh instead (as a "Before launch" step on your Debug config, or run it
# once manually) and let your existing debugpy-based Debug config launch the app.
#
# Deliberately runs uvicorn WITHOUT --reload: its two-process reloader/worker split
# can leave the reloader alive (and the port held) even when the worker's lifespan
# startup fails, which defeats a clean Stop. Use the plain Start/Debug config instead
# for fast code-reload iteration; this one is for reliably testing the real webhook.
set -euo pipefail

cd "$(dirname "$0")/.."
source "$(dirname "$0")/_tunnel_lib.sh"

PORT=8000
ENV_FILE=.env

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

start_tunnel "$PORT" "$ENV_FILE"

echo "Starting app..."
uv run uvicorn app.main:app --port "$PORT" &
APP_PID=$!
wait "$APP_PID"
