"""
VEIL Permission Broker — prototype (Stage 1: VEIL Core)

The core rule: the AI never gets a raw capability.
It asks the broker. The broker checks USER policy, then executes
— or denies — inside an isolated context. The agent only gets the result.

This prototype runs in-process for demo purposes. In the real
system the broker MUST be a separate process / user from the agent,
so a compromised agent cannot rewrite policy or skip the check.
That separation is the product — see ARCHITECTURE.md threat model.
"""
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

AUDIT_LOG = Path(__file__).parent / "audit.log.jsonl"


@dataclass
class Decision:
    allowed: bool
    reason: str
    action: str = ""
    detail: str = ""


@dataclass
class Broker:
    policy_path: Path
    policy: dict = field(init=False)

    def __post_init__(self):
        self.policy = json.loads(Path(self.policy_path).read_text())

    # -- audit ---------------------------------------------------------
    def _audit(self, action, allowed, reason, detail=""):
        entry = {
            "ts": time.time(),
            "action": action,
            "allowed": allowed,
            "reason": reason,
            "detail": detail,
        }
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def _deny(self, action, reason, detail=""):
        self._audit(action, False, reason, detail)
        return Decision(False, reason, action, detail)

    def _allow(self, action, reason, detail=""):
        self._audit(action, True, reason, detail)
        return Decision(True, reason, action, detail)

    # -- the single choke point ---------------------------------------
    def request(self, action: str, **kwargs) -> Decision:
        """Every agent capability goes through here. No exceptions."""
        handler = {
            "browser.open": self._browser_open,
            "file.read": self._file_read,
            "file.write": self._file_write,
            "terminal.run": self._terminal_run,
            "api.call": self._api_call,
            "system.settings": self._system_settings,
            "install.software": self._install_software,
            "camera.access": self._camera_access,
            "network.direct": self._network_direct,
        }.get(action)
        if handler is None:
            return self._deny(action, "unknown action — default deny")
        return handler(**kwargs)

    # -- handlers ------------------------------------------------------
    def _browser_open(self, url: str = ""):
        cfg = self.policy.get("browser", {})
        if not cfg.get("allowed"):
            return self._deny("browser.open", "browser access blocked by user policy", url)
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if host not in cfg.get("allowed_domains", []):
            return self._deny("browser.open", f"domain not allowlisted: {host}", url)
        return self._allow("browser.open", f"domain allowlisted: {host}", url)

    def _file_read(self, path: str = ""):
        cfg = self.policy.get("file_access", {})
        if not cfg.get("allowed") or not cfg.get("read"):
            return self._deny("file.read", "file access blocked by user policy", path)
        scope = cfg.get("scope", "/workspace")
        # Real sandbox: this is enforced by mount namespace, not string check.
        # String check here is the prototype's stand-in for that mount.
        if not os.path.abspath(path).startswith(scope):
            return self._deny("file.read", f"path outside workspace scope {scope}", path)
        return self._allow("file.read", "inside workspace scope", path)

    def _file_write(self, path: str = ""):
        cfg = self.policy.get("file_access", {})
        if not cfg.get("allowed") or not cfg.get("write"):
            return self._deny("file.write", "file write blocked by user policy", path)
        scope = cfg.get("scope", "/workspace")
        if not os.path.abspath(path).startswith(scope):
            return self._deny("file.write", f"path outside workspace scope {scope}", path)
        return self._allow("file.write", "inside workspace scope", path)

    def _terminal_run(self, command: str = ""):
        cfg = self.policy.get("terminal", {})
        if not cfg.get("allowed"):
            return self._deny("terminal.run", "terminal blocked by user policy", command)
        cmd = command.strip().split()[0] if command.strip() else ""
        if cmd not in cfg.get("approved_commands", []):
            return self._deny("terminal.run", f"command not approved: {cmd}", command)
        return self._allow("terminal.run", f"approved command: {cmd}", command)

    def _api_call(self, provider: str = ""):
        if not self.policy.get("api_connections", {}).get("allowed"):
            return self._deny("api.call", "API connections blocked by user policy", provider)
        return self._allow("api.call", "API provider authorised", provider)

    def _system_settings(self, setting: str = ""):
        return self._deny("system.settings", "system settings blocked by user policy", setting)

    def _install_software(self, package: str = ""):
        return self._deny("install.software", "install software blocked by user policy", package)

    def _camera_access(self, device: str = ""):
        return self._deny("camera.access", "camera/microphone blocked by user policy", device)

    def _network_direct(self, host: str = ""):
        # The bypass attempt: agent tries to go around the broker.
        # Policy says internet is broker-only, so a direct socket is denied
        # even if the domain itself would be allowlisted via the browser.
        if not self.policy.get("internet", {}).get("via_broker_only", True):
            return self._allow("network.direct", "direct network allowed", host)
        return self._deny(
            "network.direct",
            "direct network access denied — internet is broker-only",
            host,
        )
