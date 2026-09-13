#!/usr/bin/env bash
# Shared cloudflared quick-tunnel setup, sourced by dev_with_tunnel.sh and
# start_tunnel.sh - not meant to be run directly.
#
# Defines start_tunnel(port, env_file), which on success sets two globals:
#   TUNNEL_PID  - pid of the cloudflared process (nohup'd + disowned, so it survives
#                 this script exiting; the caller is responsible for killing it later
#                 if it wants the tunnel to end with the script)
#   TUNNEL_URL  - the https://....trycloudflare.com URL, already written into
#                 TELEGRAM_WEBHOOK_BASE_URL in env_file

start_tunnel() {
    local port=$1
    local env_file=$2
    local log_file
    log_file="$(mktemp -t cloudflared_log)"

    echo "Stopping any existing tunnel for this app..."
    pkill -f "cloudflared tunnel --url http://localhost:$port" 2>/dev/null || true
    sleep 1

    echo "Starting a new cloudflared tunnel..."
    nohup cloudflared tunnel --url "http://localhost:$port" > "$log_file" 2>&1 &
    TUNNEL_PID=$!
    disown "$TUNNEL_PID" 2>/dev/null || true

    echo "Waiting for tunnel URL..."
    local url=""
    for _ in $(seq 1 30); do
        if grep -qE "failed to request quick Tunnel" "$log_file" 2>/dev/null; then
            echo "cloudflared failed to create a tunnel - see $log_file:" >&2
            cat "$log_file" >&2
            return 1
        fi
        # Real quick-tunnel hostnames are always multi-word-hyphenated (e.g.
        # does-provinces-killing-customs.trycloudflare.com). Requiring a hyphen also
        # rules out matching cloudflared's OWN api.trycloudflare.com endpoint, which
        # shows up verbatim in its failure output and would otherwise look like a hit.
        url=$(grep -Eo 'https://[a-zA-Z0-9]+(-[a-zA-Z0-9]+)+\.trycloudflare\.com' "$log_file" | head -1 || true)
        if [ -n "$url" ]; then
            break
        fi
        sleep 1
    done

    if [ -z "$url" ]; then
        echo "Failed to get a tunnel URL after 30s - see $log_file" >&2
        cat "$log_file" >&2
        return 1
    fi

    echo "Tunnel URL: $url"

    # cloudflared prints the URL as soon as it's assigned, but the hostname can take a
    # few more seconds to actually become resolvable on Cloudflare's edge - registering
    # the Telegram webhook before that fails with "Failed to resolve host". Query a
    # public resolver directly (not the local one) so a cached NXDOMAIN doesn't stall this.
    local host=${url#https://}
    echo "Waiting for $host to resolve..."
    local resolved=""
    for _ in $(seq 1 20); do
        resolved=$(dig +short "$host" @1.1.1.1 2>/dev/null || true)
        if [ -n "$resolved" ]; then
            break
        fi
        sleep 1
    done
    if [ -z "$resolved" ]; then
        echo "Warning: $host still not resolving after 20s, continuing anyway..." >&2
    fi

    sed -i '' "s#^TELEGRAM_WEBHOOK_BASE_URL=.*#TELEGRAM_WEBHOOK_BASE_URL=$url#" "$env_file"
    TUNNEL_URL="$url"
}
