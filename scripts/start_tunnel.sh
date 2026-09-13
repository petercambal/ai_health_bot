#!/usr/bin/env bash
# Refreshes the cloudflared quick tunnel and updates .env's TELEGRAM_WEBHOOK_BASE_URL,
# then exits - the tunnel keeps running in the background (nohup'd + disowned).
#
# Use this before your normal debugpy-based Debug run configuration when you want
# real Telegram webhook traffic while stepping through breakpoints: a Debug config
# launches uvicorn directly so the debugger can attach, which means it can't also
# wrap a tunnel around it the way dev_with_tunnel.sh does for non-debug runs.
#
# Two ways to use it:
#   1. Run it once manually, then hit Debug.
#   2. Wire it up as a "Before launch" step on your Debug config (Edit Configurations
#      -> Before launch -> + -> Run Another Configuration -> a Shell Script config
#      pointing at this file) so one click does both.
#
# Re-run any time the tunnel dies (see the recurring trycloudflare.com DNS/edge
# flakiness noted in the README) - it kills the old one first. Since the tunnel isn't
# tied to this script's own process, stopping your Debug session does NOT stop it;
# check `ps aux | grep cloudflared` if you want to kill it between sessions.
set -euo pipefail

cd "$(dirname "$0")/.."
source "$(dirname "$0")/_tunnel_lib.sh"

PORT=8000
ENV_FILE=.env

if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE - copy .env.example to .env and fill it in first." >&2
    exit 1
fi

start_tunnel "$PORT" "$ENV_FILE"

echo ""
echo "Done - .env now points at $TUNNEL_URL"
echo "Now launch your Debug run configuration to start the app with breakpoints."
