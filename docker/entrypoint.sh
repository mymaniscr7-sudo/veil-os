#!/usr/bin/env bash
# VEIL OS container entrypoint.
# Starts, in order: Permission Broker -> video Remote API (:8766) ->
# Security Center (:8765), then waits. Runs as root inside the
# container — Bubblewrap needs uid 0 here (see Dockerfile "Why root").
# tini (PID 1) forwards SIGTERM/SIGINT here; the trap stops the children.
set -euo pipefail

cd /opt/veil

# Broker first, explicitly, so a failure is loud at boot instead of
# silent at the first proposal. The Security Center would auto-start it,
# but an explicit start also proves the policy file loads.
# (Already-running is fine — `start` is idempotent for our purposes.)
python3 veil-core/veil-broker.py start --policy veil-core/policy_v2.example.json || {
    echo "WARNING: broker did not start cleanly — see run/veil-broker.log" >&2
}

# Remote API token: use the env var if given, else generate once and keep
# the token file (run/remote-token, 0600). --gen-token prints the token
# once; that line is the Windows client's login — keep it secret.
if [ -z "${VEIL_REMOTE_TOKEN:-}" ] && [ ! -f run/remote-token ]; then
    python3 veil-video/remote_api.py --gen-token
fi

# Remote API for the Windows client (Bearer token required).
python3 veil-video/remote_api.py --host 0.0.0.0 --port 8766 &
REMOTE_PID=$!

# Security Center (broker-gated UI + approvals). Binds 0.0.0.0 so Docker
# can forward the port; compose publishes it on 127.0.0.1 only, because
# the Security Center has no auth by design.
python3 veil-desktop/server.py --host 0.0.0.0 --port 8765 &
SERVER_PID=$!

trap 'kill "$REMOTE_PID" "$SERVER_PID" 2>/dev/null; wait' TERM INT
wait
