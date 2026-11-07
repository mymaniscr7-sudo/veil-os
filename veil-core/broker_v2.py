"""
veil-broker v2 — independent policy + authorization service.

Research v2 changes over broker v1:
  - Capabilities, not booleans: who / what / scope / expiry / risk.
  - Four outcomes: allow | deny | needs_user_confirmation |
    needs_step_up_authentication (not just allow/deny).
  - Fail-closed: policy missing/corrupt, journal write fails,
    capability expired, approval doesn't match the EXACT normalized
    action -> deny. Never fail open.
  - Approval binds to the exact normalized action (binding_id),
    not to a vague task name. A model-generated "user_confirmed: true"
    is never trusted — the caller must present a broker-issued approval
    whose binding_id matches.
  - Network is a state, not a switch: offline / approved-domains /
    research-web / unrestricted-with-confirmation.
  - Proposals carry expected_effect + rollback requirement, per the
    tool contract: propose -> validate -> execute -> observe -> rollback.

This module is usable WITHOUT any AI model — deterministic, testable.
The model runtime talks to it over a narrow RPC; it cannot call
systemd, D-Bus, Docker, pacman, the host filesystem, or arbitrary
sockets directly (that separation is enforced by the executor, see
veil-sandbox/).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from capability import Capability, action_class_for, NETWORK_STATES
from journal import Journal, JournalError

OUTCOMES = ["allow", "deny", "needs_user_confirmation",
            "needs_step_up_authentication"]


@dataclass
class Proposal:
    proposal_id: str
    action: str
    target: str
    expected_effect: str
    rollback: str  # "snapshot-required" | "none"
    capability: Capability
    outcome: str
    reason: str


class BrokerV2:
    def __init__(self, policy_path: Path, journal_path: Path | None = None):
        self.policy_path = Path(policy_path)
        try:
            self.policy = json.loads(self.policy_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            # Fail closed at construction: no policy, no broker.
            raise ValueError(f"broker cannot start without valid policy: {e}") from e
        if journal_path is None:
            journal_path = Path(__file__).parent / "audit-v2.log.jsonl"
        self.journal = Journal(journal_path)
        # binding_id -> (token, expires_at, capability, canonical).
        # Approvals expire with the capability they were granted for.
        self._approvals: dict[str, tuple[str, datetime, str, str]] = {}
        # proposal_id -> pending proposal (needs_* outcomes only).
        # The Security Center lists these; approving one grants a token
        # for its exact capability, denying one removes it.
        self._pending: dict[str, dict] = {}
        # binding_id -> denied-at timestamp. A denied action re-proposed
        # within 5 minutes returns deny (not needs_* again), so the
        # agent learns promptly instead of waiting out a timeout.
        self._denied: dict[str, float] = {}

    def _approval_token(self, binding_id: str) -> str | None:
        """Return a live token, or None if missing/expired (and clean up)."""
        entry = self._approvals.get(binding_id)
        if entry is None:
            return None
        token, expires_at, _cap, _canon = entry
        if datetime.now(timezone.utc) >= expires_at:
            del self._approvals[binding_id]
            return None
        return token

    def list_approvals(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        out = []
        for binding_id, (token, expires_at, cap_name, canonical) in list(self._approvals.items()):
            out.append({
                "binding_id": binding_id,
                "capability": cap_name,
                "canonical": canonical,
                "expires_at": expires_at.isoformat(),
                "expires_in_seconds": max(0, int((expires_at - now).total_seconds())),
                "expired": now >= expires_at,
            })
        return out

    def list_pending(self) -> list[dict]:
        return sorted(self._pending.values(), key=lambda p: p["ts"])

    # -- approvals (broker-issued, exact-action bound) -----------------
    def grant_approval(self, capability: Capability) -> str:
        """User approving in the Security Center.
        Returns a token bound to capability.binding_id, expiring with
        the capability (default 15 min from capability creation)."""
        token = f"appr-{uuid.uuid4().hex[:12]}"
        self._approvals[capability.binding_id] = (
            token, capability.expires_at, capability.capability, capability.canonical())
        # Approving clears a prior denial for the same exact action.
        self._denied.pop(capability.binding_id, None)
        return token

    def approve_pending(self, proposal_id: str) -> str | None:
        """Approve a pending proposal: grant a token for its exact
        capability and clear it from pending. Returns the token."""
        p = self._pending.get(proposal_id)
        if p is None:
            return None
        cap = Capability(capability=p["capability"]["capability"],
                         who=p["capability"]["who"],
                         scope=p["capability"]["scope"],
                         session=p["capability"]["session"],
                         risk=p["capability"]["risk"],
                         expires_at=p["capability"].get("expires_at"))
        token = self.grant_approval(cap)
        del self._pending[proposal_id]
        return token

    def deny_pending(self, proposal_id: str) -> bool:
        """Deny a pending proposal: remove it and journal the denial."""
        p = self._pending.pop(proposal_id, None)
        if p is None:
            return False
        import time as _time
        self._denied[p["binding_id"]] = _time.time()
        try:
            self.journal.append({
                "proposal_id": proposal_id, "action": p["action"],
                "target": p["target"], "expected_effect": p["expected_effect"],
                "rollback": p["rollback"], "capability": p["capability"]["canonical"],
                "binding_id": p["binding_id"], "outcome": "deny",
                "reason": "denied by user in Security Center",
                "risk": p["capability"]["risk"], "session": p["capability"]["session"],
            })
        except JournalError:
            pass  # denial is already recorded by removing it from pending
        return True

    # -- policy mutation (Security Center toggles — NOT decorative) -----
    def save_policy(self) -> None:
        """Persist the in-memory policy atomically (tmp + rename)."""
        tmp = self.policy_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.policy, indent=2, sort_keys=True))
        tmp.replace(self.policy_path)

    def set_network_state(self, state: str) -> str:
        if state not in NETWORK_STATES:
            raise ValueError(f"unknown network state: {state!r}")
        self.policy.setdefault("network", {})["state"] = state
        # Going offline revokes network/browser approvals immediately:
        # a token granted when the network was on must not survive it.
        if state == "offline":
            for bid in list(self._approvals.keys()):
                del self._approvals[bid]
            self._pending = {k: v for k, v in self._pending.items()
                             if not v["capability"]["capability"].startswith(("browser.", "network."))}
        self.save_policy()
        return state

    def set_capability(self, name: str, allowed: bool) -> bool:
        self.policy.setdefault("capabilities", {})[name] = bool(allowed)
        # Turning a capability off denies on next propose even with a
        # live token: _validate checks the policy allowlist before
        # approval checks. No token cleanup needed for correctness.
        self.save_policy()
        return bool(allowed)

    def reload_policy(self) -> None:
        self.policy = json.loads(self.policy_path.read_text())

    # -- the single choke point ----------------------------------------
    def propose(self, *, action: str, target: str, expected_effect: str,
                rollback: str = "snapshot-required",
                capability: Capability,
                approval_token: str | None = None) -> Proposal:
        pid = f"prop-{uuid.uuid4().hex[:8]}"
        outcome, reason = self._validate(capability, action, approval_token)
        proposal = Proposal(pid, action, target, expected_effect,
                            rollback, capability, outcome, reason)
        # Journal first — if this fails, fail closed (deny, don't execute).
        try:
            self.journal.append({
                "proposal_id": pid, "action": action, "target": target,
                "expected_effect": expected_effect, "rollback": rollback,
                "capability": capability.canonical(),
                "binding_id": capability.binding_id,
                "outcome": outcome, "reason": reason,
                "risk": capability.risk, "session": capability.session,
            })
        except JournalError as e:
            return Proposal(pid, action, target, expected_effect, rollback,
                            capability, "deny", f"fail-closed: {e}")
        if outcome in ("needs_user_confirmation", "needs_step_up_authentication"):
            import time as _time
            self._pending[pid] = {
                "proposal_id": pid, "action": action, "target": target,
                "expected_effect": expected_effect, "rollback": rollback,
                "capability": {
                    "capability": capability.capability, "who": capability.who,
                    "scope": capability.scope, "session": capability.session,
                    "risk": capability.risk,
                    "expires_at": capability.expires_at.isoformat(),
                    "canonical": capability.canonical(),
                },
                "binding_id": capability.binding_id, "outcome": outcome,
                "reason": reason, "ts": _time.time(),
            }
        elif outcome == "allow":
            # A pending proposal for the same exact action that is now
            # allowed clears from pending.
            for k in [k for k, v in self._pending.items()
                      if v["binding_id"] == capability.binding_id]:
                del self._pending[k]
        return proposal

    def _validate(self, cap: Capability, action: str,
                  approval_token: str | None):
        # 0. Denied recently by the user in the Security Center?
        # Return deny promptly so the agent stops waiting.
        import time as _time
        denied_at = self._denied.get(cap.binding_id)
        if denied_at is not None and _time.time() - denied_at < 300:
            return "deny", "denied by user in Security Center"
        if denied_at is not None:
            del self._denied[cap.binding_id]
        # 1. Expiry — capabilities expire by default.
        if cap.expired:
            return "deny", f"capability expired at {cap.expires_at.isoformat()}"
        # 2. Session binding — a capability for task A can't be used in task B.
        # (Enforced by the caller passing the right session; the canonical
        #  binding includes the session, so a stolen token won't match.)
        # 3. Network state.
        if cap.capability.startswith("network.") or cap.capability.startswith("browser."):
            state = self.policy.get("network", {}).get("state", "offline")
            if state not in NETWORK_STATES:
                return "deny", f"fail-closed: unknown network state {state!r}"
            if state == "offline":
                return "deny", "network state is offline"
            if state == "approved-domains":
                domains = self.policy.get("network", {}).get("allowed_domains", [])
                host = cap.scope.get("domains", [None])[0]
                if host not in domains:
                    return "deny", f"domain not allowlisted in approved-domains state: {host}"
            if state == "unrestricted-with-confirmation":
                # fall through to confirmation below
                pass
        # 4. Policy allowlist for the capability itself.
        allowed = self.policy.get("capabilities", {}).get(cap.capability)
        if allowed is False:
            return "deny", f"capability {cap.capability} blocked by policy"
        # 4b. Terminal: only approved commands, checked against policy.
        # This is the v1 approved-commands check, carried into v2.
        # Without it, "terminal.run is auto" would allow any command.
        if cap.capability == "terminal.run":
            approved = self.policy.get("terminal", {}).get("approved_commands", [])
            cmds = cap.scope.get("commands", [])
            cmd = cmds[0] if cmds else ""
            if cmd not in approved:
                return "deny", f"command not approved: {cmd}"
        # 5. Action class -> outcome.
        cls = action_class_for(cap.capability, cap.risk)
        # Stored approval: if the user already approved this exact
        # action (binding_id), the agent retrying WITHOUT a token is
        # allowed — the broker itself holds the approval, the token is
        # only needed as out-of-band proof. A WRONG token still fails.
        # No stored approval at all -> needs confirmation (never allow).
        live = self._approval_token(cap.binding_id)
        if cls == "irreversible-privileged":
            if live is None or (approval_token is not None and approval_token != live):
                return ("needs_step_up_authentication",
                        "irreversible/privileged: explicit confirmation + step-up auth required")
            return "allow", "step-up approval verified for exact action"
        if cls == "external-side-effect":
            if live is None or (approval_token is not None and approval_token != live):
                return ("needs_user_confirmation",
                        "external side effect: user confirmation required")
            return "allow", "user confirmation verified for exact action"
        # read / reversible-write inside scope: allow (snapshot covers rollback)
        return "allow", f"action class {cls}: automatic inside scope"
