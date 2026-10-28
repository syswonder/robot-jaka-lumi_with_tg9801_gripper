#!/usr/bin/env bash
# Host prep + zenoh router + rbnx boot for the JAKA Lumi deployment.
#
# Usage:
#   bash start.sh [manifest]     # default: robonix_manifest.local.yaml
#                                # (falls back to robonix_manifest.yaml)
#
# The rmw_zenohd router is owned by this script (PID file in rbnx-boot/);
# a router already running and tracked by that PID file is reused, anything
# else on port 7447 is left alone.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
ROS_SETUP="${RBNX_ROS_SETUP:-/opt/ros/humble/setup.bash}"
DEFAULT_MANIFEST="$DEPLOY_DIR/robonix_manifest.local.yaml"
[[ -f "$DEFAULT_MANIFEST" ]] || DEFAULT_MANIFEST="$DEPLOY_DIR/robonix_manifest.yaml"
MANIFEST="${1:-$DEFAULT_MANIFEST}"

# Runtime environment for every boot child (ROS middleware vars, deployment
# venv on PATH) lives in the manifest `env:` block — rbnx boot applies it.
# This script only owns what the manifest cannot express: the rmw_zenohd
# lifecycle and the gitignored secrets below.

# VLM credentials for Pilot and the head camera serial. rbnx fails with a
# clear message if the VLM vars are still unset at boot.
if [[ -f "$DEPLOY_DIR/.vlmkey" ]]; then
    # shellcheck disable=SC1091
    source "$DEPLOY_DIR/.vlmkey"
fi

cd "$DEPLOY_DIR"
mkdir -p rbnx-boot/logs

# rmw_zenohd and every ROS 2 process below need the ROS library paths;
# relax `set -u` while sourcing setup.bash.
set +u
# shellcheck disable=SC1090
source "$ROS_SETUP"
set -u

ZENOH_ROUTER_BIN="/opt/ros/${ROS_DISTRO:-humble}/lib/rmw_zenoh_cpp/rmw_zenohd"
ZENOH_ROUTER_PID_FILE="rbnx-boot/rmw_zenohd.pid"
ZENOH_ROUTER_EXPECTED="$(readlink -f "$ZENOH_ROUTER_BIN")"
ROUTER_OWNED=0

router_alive_and_ours() {
    test -f "$ZENOH_ROUTER_PID_FILE" || return 1
    pid="$(cat "$ZENOH_ROUTER_PID_FILE")"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    actual="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
    test "$actual" = "$ZENOH_ROUTER_EXPECTED"
}

stop_owned_router() {
    if [[ "$ROUTER_OWNED" == 1 ]] && router_alive_and_ours; then
        pid="$(cat "$ZENOH_ROUTER_PID_FILE")"
        kill -TERM "$pid"
        for _ in $(seq 1 20); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.1
        done
        kill -KILL "$pid" 2>/dev/null || true
    fi
    if [[ "$ROUTER_OWNED" == 1 ]]; then
        rm -f "$ZENOH_ROUTER_PID_FILE"
    fi
}

cleanup() {
    stop_owned_router
    exit 130
}
trap cleanup INT TERM
trap stop_owned_router EXIT

router_port_open() {
    python3 - <<'PY'
import socket
with socket.create_connection(("127.0.0.1", 7447), timeout=0.2):
    pass
PY
}

if router_alive_and_ours; then
    echo "[start] reusing running rmw_zenohd (pid $(cat "$ZENOH_ROUTER_PID_FILE"))"
elif router_port_open; then
    # e.g. a systemd unit owns it — not pid-file tracked, so leave it alone.
    echo "[start] reusing external Zenoh router on 127.0.0.1:7447"
else
    rm -f "$ZENOH_ROUTER_PID_FILE"  # stale pid file
    "$ZENOH_ROUTER_BIN" >rbnx-boot/logs/rmw_zenohd.log 2>&1 &
    echo "$!" >"$ZENOH_ROUTER_PID_FILE"
    ROUTER_OWNED=1

    ready=0
    for _ in $(seq 1 20); do
        if router_port_open; then
            ready=1
            break
        fi
        sleep 0.25
    done
    kill -0 "$(cat "$ZENOH_ROUTER_PID_FILE")"
    test "$ready" -eq 1 || {
        tail -n 80 rbnx-boot/logs/rmw_zenohd.log
        exit 1
    }
    echo "[start] rmw_zenohd ready on tcp/127.0.0.1:7447"
fi

# rbnx boot and its children (soma runtime reader, package start.sh scripts)
# inherit the ROS environment sourced above. --no-update-check skips the
# pre-boot per-package git fetch (offline / pinned cache; refresh via
# `rbnx update`).
rbnx boot -v --no-update-check -f "$MANIFEST"
