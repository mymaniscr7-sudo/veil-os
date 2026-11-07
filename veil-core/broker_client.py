"""
broker_client — thin RPC client for the AGENT side. No policy, no
broker code, no journal. It only knows how to send JSON to a socket.

This file deliberately imports NOTHING from broker_v2, capability,
journal, or any policy file. If the agent process is compromised,
it still cannot read policy, grant itself approval, or skip the
broker — it can only ask, over the agent socket.

Fail-closed contract:
  - broker down / socket missing / timeout / bad response ->
    BrokerUnavailable. Callers MUST deny and NOT execute.
  - a "deny" / "needs_*" outcome is not an error — it is a decision.
    Only transport failures raise.

Usage (agent):
  from broker_client import BrokerClient, BrokerUnavailable
  client = BrokerClient()  # defaults to ../run/veil-broker-agent.sock
  r = client.propose(action="filesystem.write", target="/workspace/x.py",
                     expected_effect="create one file",
                     capability={"capability": "filesystem.write",
                                 "who": "agent:task-1",
                                 "scope": {"paths": ["/workspace/**"]},
                                 "session": "task-1", "risk": "medium"})
  # r -> {"proposal_id": ..., "outcome": "allow"|"deny"|..., "reason": ...}
"""
from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path

DEFAULT_RUN_DIR = Path(__file__).parent.parent / "run"


class BrokerUnavailable(Exception):
    """Transport failure — broker down, socket gone, timeout, bad reply.
    Callers must fail closed: deny, do not execute."""


class BrokerClient:
    def __init__(self, agent_sock: Path | None = None,
                 run_dir: Path | None = None, timeout: float = 5.0):
        if agent_sock is None:
            run_dir = Path(run_dir) if run_dir else DEFAULT_RUN_DIR
            agent_sock = Path(run_dir) / "veil-broker-agent.sock"
        self.agent_sock = Path(agent_sock)
        self.timeout = timeout

    def _call(self, method: str, params: dict | None = None) -> dict:
        req = {"method": method, "params": params or {}, "id": uuid.uuid4().hex[:8]}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(str(self.agent_sock))
                s.sendall((json.dumps(req) + "\n").encode())
                data = b""
                while not data.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout,
                OSError) as e:
            raise BrokerUnavailable(f"broker unreachable at {self.agent_sock}: {e}") from e
        try:
            resp = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError) as e:
            raise BrokerUnavailable(f"broker returned bad reply: {e}") from e
        if "error" in resp:
            raise BrokerUnavailable(f"broker error: {resp['error']}")
        return resp.get("result", {})

    def ping(self) -> dict:
        return self._call("ping")

    def propose(self, *, action: str, target: str, expected_effect: str,
                rollback: str = "snapshot-required",
                capability: dict, approval_token: str | None = None) -> dict:
        return self._call("propose", {
            "action": action, "target": target,
            "expected_effect": expected_effect, "rollback": rollback,
            "capability": capability, "approval_token": approval_token,
        })


class AdminClient:
    """User / Security Center side only. Never import this in agent code.
    Talks to the admin socket (0600)."""
    def __init__(self, admin_sock: Path | None = None,
                 run_dir: Path | None = None, timeout: float = 5.0):
        if admin_sock is None:
            run_dir = Path(run_dir) if run_dir else DEFAULT_RUN_DIR
            admin_sock = Path(run_dir) / "veil-broker-admin.sock"
        self.admin_sock = Path(admin_sock)
        self.timeout = timeout

    def _call(self, method: str, params: dict | None = None) -> dict:
        req = {"method": method, "params": params or {}, "id": uuid.uuid4().hex[:8]}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(str(self.admin_sock))
                s.sendall((json.dumps(req) + "\n").encode())
                data = b""
                while not data.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout,
                OSError) as e:
            raise BrokerUnavailable(f"admin broker unreachable at {self.admin_sock}: {e}") from e
        resp = json.loads(data.decode())
        if "error" in resp:
            raise BrokerUnavailable(f"broker error: {resp['error']}")
        return resp.get("result", {})

    def grant_approval(self, capability: dict) -> dict:
        return self._call("grant_approval", {"capability": capability})

    def revoke_approval(self, capability: dict) -> dict:
        return self._call("revoke_approval", {"capability": capability})

    def status(self) -> dict:
        return self._call("status")

    def list_pending(self) -> dict:
        return self._call("list_pending")

    def list_approvals(self) -> dict:
        return self._call("list_approvals")

    def approve_pending(self, proposal_id: str) -> dict:
        return self._call("approve_pending", {"proposal_id": proposal_id})

    def deny_pending(self, proposal_id: str) -> dict:
        return self._call("deny_pending", {"proposal_id": proposal_id})

    def set_network_state(self, state: str) -> dict:
        return self._call("set_network_state", {"state": state})

    def set_capability(self, capability: str, allowed: bool) -> dict:
        return self._call("set_capability", {"capability": capability, "allowed": allowed})

    def get_policy(self) -> dict:
        return self._call("get_policy")

    def reload_policy(self) -> dict:
        return self._call("reload_policy")

    def shutdown(self) -> dict:
        return self._call("shutdown")
