"""
veil-broker — separate process (Stage 1 hardening)

The broker is NO LONGER imported by the agent. It runs as its own
process, owns the policy file and the journal, and talks to agents
over a narrow JSON-RPC interface on a Unix socket.

Two sockets, two trust levels — in ONE broker process, but with
different method allowlists:

  agent socket  (veil-broker-agent.sock, 0660)
      propose, ping
      — the agent can ASK. It cannot grant, revoke, read policy,
        or shut the broker down.

  admin socket  (veil-broker-admin.sock, 0600)
      grant_approval, revoke_approval, status, shutdown
      — the user / Security Center only.

Separation in this prototype is by socket + method allowlist.
In production the broker runs as a dedicated user (e.g. `veil`),
the agent as `veil-agent`, and the admin socket is 0600 owned by
`veil` — so even a compromised agent process cannot open the admin
socket. On this dev host everything runs as the same UID, so the
socket-permission layer is documented, not pretend-enforced.
The method allowlist IS enforced here, in this process, regardless
of UID — that part is real.

Fail-closed:
  - broker down / socket gone -> client raises BrokerUnavailable,
    callers must deny and NOT execute. No in-process fallback.
  - policy missing/corrupt -> broker refuses to start.
  - journal write fails -> propose returns deny.
  - malformed request / unknown method -> error, broker stays up.
  - approvals are in-memory only. Restart the broker and approvals
    are gone — the agent must ask again. That is intentional.

Protocol: one JSON object per connection, newline-terminated.
  request:  {"method": "propose", "params": {...}, "id": "…"}
  response: {"id": "…", "result": {...}}  or  {"id": "…", "error": {"code": …, "message": …}}

Run:
  python3 broker_service.py --serve --run-dir ../run
  python3 veil-broker.py start | stop | status | grant | propose
"""
from __future__ import annotations

import json
import os
import signal
import socket
import socketserver
import sys
import threading
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from broker_v2 import BrokerV2  # noqa: E402
from capability import Capability  # noqa: E402

AGENT_METHODS = {"propose", "ping"}
ADMIN_METHODS = {"grant_approval", "revoke_approval", "status", "shutdown", "ping",
                 "list_pending", "list_approvals", "approve_pending", "deny_pending",
                 "set_network_state", "set_capability", "get_policy", "reload_policy"}


class BrokerService:
    def __init__(self, policy_path: Path, run_dir: Path):
        self.policy_path = Path(policy_path)
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.agent_sock = self.run_dir / "veil-broker-agent.sock"
        self.admin_sock = self.run_dir / "veil-broker-admin.sock"
        self.pid_file = self.run_dir / "veil-broker.pid"
        self.log_file = self.run_dir / "veil-broker.log"
        # BrokerV2 raises ValueError if policy is missing/corrupt:
        # fail closed at startup, before any socket is created.
        self.broker = BrokerV2(self.policy_path)
        self._shutdown = threading.Event()
        self._servers: list[socketserver.UnixStreamServer] = []

    # -- request handling ------------------------------------------------
    def _cap_from_dict(self, d: dict) -> Capability:
        return Capability(
            capability=d["capability"],
            who=d.get("who", "agent:unknown"),
            scope=d.get("scope", {}),
            session=d.get("session", "unknown"),
            risk=d.get("risk", "medium"),
            expires_at=d.get("expires_at"),
            approval=d.get("approval", "auto"),
        )

    def handle(self, channel: str, req: dict) -> dict:
        method = req.get("method")
        params = req.get("params") or {}
        allowed = AGENT_METHODS if channel == "agent" else ADMIN_METHODS
        if method not in allowed:
            return {"error": {"code": "method_not_allowed",
                              "message": f"{method!r} is not allowed on the {channel} socket"}}
        try:
            if method == "ping":
                return {"result": {"ok": True, "channel": channel}}
            if method == "propose":
                cap = self._cap_from_dict(params["capability"])
                prop = self.broker.propose(
                    action=params["action"],
                    target=params.get("target", ""),
                    expected_effect=params.get("expected_effect", ""),
                    rollback=params.get("rollback", "snapshot-required"),
                    capability=cap,
                    approval_token=params.get("approval_token"),
                )
                return {"result": {
                    "proposal_id": prop.proposal_id,
                    "action": prop.action,
                    "target": prop.target,
                    "outcome": prop.outcome,
                    "reason": prop.reason,
                    "binding_id": cap.binding_id,
                }}
            if method == "grant_approval":
                cap = self._cap_from_dict(params["capability"])
                token = self.broker.grant_approval(cap)
                return {"result": {"token": token, "binding_id": cap.binding_id}}
            if method == "revoke_approval":
                cap = self._cap_from_dict(params["capability"])
                self.broker._approvals.pop(cap.binding_id, None)
                return {"result": {"revoked": True, "binding_id": cap.binding_id}}
            if method == "status":
                return {"result": {
                    "ok": True,
                    "policy": str(self.policy_path),
                    "approvals_held": len(self.broker._approvals),
                    "pending": len(self.broker._pending),
                    "pid": os.getpid(),
                }}
            if method == "list_pending":
                return {"result": {"pending": self.broker.list_pending()}}
            if method == "list_approvals":
                return {"result": {"approvals": self.broker.list_approvals()}}
            if method == "approve_pending":
                token = self.broker.approve_pending(params["proposal_id"])
                if token is None:
                    return {"error": {"code": "not_found", "message": "no such pending proposal"}}
                return {"result": {"token": token, "proposal_id": params["proposal_id"]}}
            if method == "deny_pending":
                ok = self.broker.deny_pending(params["proposal_id"])
                if not ok:
                    return {"error": {"code": "not_found", "message": "no such pending proposal"}}
                return {"result": {"denied": True, "proposal_id": params["proposal_id"]}}
            if method == "set_network_state":
                state = self.broker.set_network_state(params["state"])
                return {"result": {"network_state": state, "policy": self.broker.policy.get("network")}}
            if method == "set_capability":
                allowed = self.broker.set_capability(params["capability"], bool(params["allowed"]))
                return {"result": {"capability": params["capability"], "allowed": allowed}}
            if method == "get_policy":
                return {"result": {"policy": self.broker.policy}}
            if method == "reload_policy":
                self.broker.reload_policy()
                return {"result": {"reloaded": True, "policy": self.broker.policy}}
            if method == "shutdown":
                threading.Thread(target=self._delayed_shutdown, daemon=True).start()
                return {"result": {"shutting_down": True}}
        except KeyError as e:
            return {"error": {"code": "bad_request", "message": f"missing field: {e}"}}
        except Exception as e:  # never leak a stack trace as an allow
            return {"error": {"code": "internal", "message": f"{type(e).__name__}: {e}"}}
        return {"error": {"code": "unknown", "message": "unreachable"}}

    def _delayed_shutdown(self):
        import time
        time.sleep(0.2)
        self.stop()

    # -- servers ---------------------------------------------------------
    def _make_server(self, channel: str, sock_path: Path, mode: int):
        service = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    line = self.rfile.readline(65536)
                    if not line:
                        return
                    req = json.loads(line.decode())
                    resp = service.handle(channel, req)
                    resp["id"] = req.get("id")
                    self.wfile.write((json.dumps(resp) + "\n").encode())
                except Exception as e:
                    try:
                        err = {"id": None, "error": {"code": "bad_json", "message": str(e)}}
                        self.wfile.write((json.dumps(err) + "\n").encode())
                    except Exception:
                        pass

        if sock_path.exists():
            sock_path.unlink()
        server = socketserver.ThreadingUnixStreamServer(str(sock_path), Handler)
        server.daemon_threads = True
        os.chmod(sock_path, mode)
        self._servers.append(server)
        return server

    def serve_forever(self):
        agent_server = self._make_server("agent", self.agent_sock, 0o660)
        admin_server = self._make_server("admin", self.admin_sock, 0o600)
        self.pid_file.write_text(str(os.getpid()))
        threads = [
            threading.Thread(target=agent_server.serve_forever, daemon=True),
            threading.Thread(target=admin_server.serve_forever, daemon=True),
        ]
        for t in threads:
            t.start()
        print(f"veil-broker pid={os.getpid()} agent={self.agent_sock} admin={self.admin_sock}", flush=True)
        try:
            while not self._shutdown.is_set():
                self._shutdown.wait(0.5)
        finally:
            self.stop()

    def stop(self):
        self._shutdown.set()
        for s in self._servers:
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
        for p in (self.agent_sock, self.admin_sock, self.pid_file):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--serve", action="store_true")
    p.add_argument("--policy", default=str(HERE / "policy_v2.example.json"))
    p.add_argument("--run-dir", default=str(HERE.parent / "run"))
    args = p.parse_args()
    if not args.serve:
        print("use: python3 broker_service.py --serve [--policy …] [--run-dir …]")
        return 2
    service = BrokerService(Path(args.policy), Path(args.run_dir))
    # SIGTERM/SIGINT -> clean shutdown, sockets removed.
    signal.signal(signal.SIGTERM, lambda *_: service.stop())
    signal.signal(signal.SIGINT, lambda *_: service.stop())
    service.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
