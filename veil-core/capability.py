"""
VEIL capability schema — v2 (research v2)

A capability needs five properties, not a boolean switch:
  who, what, which resources, how long, what risk class.
Capabilities expire by default and are bound to a task/workspace,
never permanent global settings.

Example:
  {
    "capability": "browser.navigate",
    "who": "agent:task-8f21",
    "scope": {"domains": ["docs.example.com", "github.com"],
              "methods": ["GET", "POST"]},
    "session": "task-8f21",
    "expires_at": "2026-11-06T01:00:00Z",
    "approval": "required-for-write",
    "risk": "medium"
  }
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def _now():
    return datetime.now(timezone.utc)


@dataclass
class Capability:
    capability: str          # e.g. "filesystem.write"
    who: str                 # e.g. "agent:task-8f21"
    scope: dict              # resources: paths, domains, methods, commands...
    session: str             # task/workspace id this is bound to
    risk: str = "medium"     # low | medium | high | critical
    expires_at: datetime | None = None
    approval: str = "auto"   # auto | required-for-write | required | step-up

    def __post_init__(self):
        if self.expires_at is None:
            # Expire by default: 15 minutes (research: 900s example)
            self.expires_at = _now() + timedelta(seconds=900)
        if isinstance(self.expires_at, str):
            self.expires_at = datetime.fromisoformat(
                self.expires_at.replace("Z", "+00:00"))

    @property
    def expired(self) -> bool:
        return _now() >= self.expires_at

    def canonical(self) -> str:
        """Exact normalized action — approval binds to THIS, not a task name."""
        return json.dumps({
            "capability": self.capability,
            "who": self.who,
            "scope": self.scope,
            "session": self.session,
            "risk": self.risk,
        }, sort_keys=True, separators=(",", ":"))

    @property
    def binding_id(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()[:16]


# Four action classes (research v2)
ACTION_CLASSES = {
    "read": "automatic inside the workspace",
    "reversible-write": "automatic inside a scoped workspace, with snapshot",
    "external-side-effect": "confirmation required (send, publish, change account)",
    "irreversible-privileged": "explicit confirmation + step-up auth "
                               "(delete, change permissions, install host "
                               "software, money, deploy)",
}

# Network policy states — a simple "internet allowed" switch is not enough.
NETWORK_STATES = ["offline", "approved-domains", "research-web",
                  "unrestricted-with-confirmation"]


def action_class_for(capability: str, risk: str) -> str:
    if capability.startswith(("filesystem.read", "browser.read", "browser.screenshot",
                              "video.read")):
        return "read"
    if risk in ("critical",):
        return "irreversible-privileged"
    if capability.startswith(("network.", "api.", "browser.navigate", "browser.act",
                              "video.export", "video.publish")):
        # video.export / video.publish move a file OUT of the project:
        # an external side effect, needs user confirmation every time.
        return "external-side-effect"
    if capability.startswith("video.delete"):
        # Deleting source media is irreversible in practice; deleting
        # a render is not. Callers set risk=critical for source
        # media (see video_tool.delete_media); anything else here
        # still needs confirmation, never auto.
        return ("irreversible-privileged" if risk == "critical"
                else "external-side-effect")
    if capability.startswith(("filesystem.write", "video.edit",
                              "video.render", "video.transcribe")):
        # video.edit / render / transcribe write only inside the
        # project workspace, with snapshot/rollback: reversible-write.
        return "reversible-write"
    if capability.startswith("terminal.run"):
        # Approved commands only, inside the sandbox, with snapshot.
        # The broker still checks the command against the policy's
        # approved list (see broker_v2) — this class only says the
        # outcome is automatic once that check passes.
        return "reversible-write"
    return "external-side-effect"
