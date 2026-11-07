"""
VEIL browser tool — the AI's browser is veil-browser-cli, not a GUI.

Why a CLI browser (the user's idea, and it's the right one):
  - An AI reading a screen and clicking pixels is slow, token-hungry
    and fragile. An AI reading a compact accessibility tree and
    acting on stable @eN refs is fast, cheap and deterministic.
  - `snapshot` to see, `@e1` to act. No screenshots by default,
    no HTML soup, no guessing selectors.
  - This is VEIL's tool-hierarchy level 2 (accessibility tree),
    exposed as level 1 (a semantic CLI) — no level-4 screenshot
    fallback unless the agent explicitly asks for a screenshot.

How it is gated (the OS part):
  - Every navigation / action is proposed to the broker FIRST.
    The domain is checked against policy BEFORE Chrome ever
    launches. A denied domain never starts a browser.
  - One CLI session per VEIL task (`--session veil-<task>`), which
    the CLI maps to its own directory with its own daemon socket,
    Chrome profile, cookies and tabs. Tasks never share a browser.
  - No host profile: the CLI uses a fresh per-session profile.
    No host cookies, passwords, SSH or personal browsing data.
  - Screenshots and downloads go to the task workspace only.
  - Login / upload / publish / purchase / send / consent are
    browser.act (external side effect) — they need user
    confirmation in the Security Center, every time.
  - Network state is enforced by the broker: offline denies all
    browser.* capabilities before any CLI call.

What this tool does NOT do:
  - It does not run Chrome inside Bubblewrap (v1). Chrome runs as
    its own process with its own sandbox, under a per-task profile,
    broker-gated. Running it inside bwrap/microVM is a later
    hardening step (see ARCHITECTURE.md).
  - It does not do downloads, file uploads or login automation —
    the CLI has no such primitives, by design. Those are broker
    decisions, not browser primitives.

Usage:
  b = VeilBrowser(session="researcher-1", workspace=Path("..."))
  decision, result = b.open("https://github.com")
  # decision.outcome == "needs_user_confirmation" -> show in Security
  # Center; after the user approves, call b.open again (stored
  # approval lets the retry through) or pass the approval token.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from broker_client import BrokerClient, BrokerUnavailable  # noqa: E402

CLI_REPO = Path.home() / "workspace" / "veil-browser-cli"
# Prefer release, then debug, then PATH. Fail closed if none exists.
_CANDIDATES = [
    CLI_REPO / "target" / "release" / "veil-browser",
    CLI_REPO / "target" / "debug" / "veil-browser",
]


def _cli_base_dir() -> Path:
    """Where the CLI keeps sessions (daemon.sock, daemon.log).

    Mirrors veil-browser-cli's config::base_dir(): VEIL_BROWSER_HOME
    wins, else $HOME/.cache/veil-browser. The runtime runs as root
    (HOME=/root) but VEIL_BROWSER_HOME points at /home/veil, so
    Path.home() alone looks in the wrong place and every
    _current_host() check reports "no page open" even after a
    successful navigate."""
    v = os.environ.get("VEIL_BROWSER_HOME")
    if v:
        return Path(v)
    return Path.home() / ".cache" / "veil-browser"


def find_cli_binary() -> Path | None:
    for c in _CANDIDATES:
        if c.exists() and c.is_file():
            return c
    import shutil
    p = shutil.which("veil-browser")
    return Path(p) if p else None


@dataclass
class BrowserDecision:
    allowed: bool
    outcome: str
    reason: str


class VeilBrowser:
    def __init__(self, session: str, workspace: Path | None = None,
                 broker_run_dir: Path | None = None,
                 cli_binary: Path | None = None,
                 timeout: int = 30):
        self.session = session
        # CLI session namespaced so VEIL tasks can't collide with a
        # human's own veil-browser sessions.
        self.cli_session = f"veil-{session}"
        self.workspace = Path(workspace).resolve() if workspace else None
        if self.workspace:
            self.workspace.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.cli = cli_binary or find_cli_binary()
        if self.cli is None:
            raise FileNotFoundError(
                "veil-browser CLI not found: build veil-browser-cli "
                "(cargo build --release) or put veil-browser on PATH")
        self.broker = BrokerClient(run_dir=broker_run_dir) if broker_run_dir else BrokerClient()

    # -- internals -----------------------------------------------------
    def _cap(self, capability: str, domains: list[str], risk: str = "medium") -> dict:
        return {"capability": capability, "who": f"agent:{self.session}",
                "scope": {"domains": domains}, "session": self.session, "risk": risk}

    def _propose(self, action: str, target: str, effect: str,
                 capability: str, domains: list[str], risk: str = "medium",
                 approval_token: str | None = None) -> BrowserDecision:
        try:
            r = self.broker.propose(action=action, target=target,
                                    expected_effect=effect,
                                    capability=self._cap(capability, domains, risk),
                                    approval_token=approval_token)
        except BrokerUnavailable as e:
            return BrowserDecision(False, "deny", f"fail-closed: broker unavailable: {e}")
        return BrowserDecision(r["outcome"] == "allow", r["outcome"], r["reason"])

    def _run_cli(self, args: list[str]) -> dict:
        """Run the CLI with --json. Only called AFTER the broker allowed."""
        cmd = [str(self.cli), "--session", self.cli_session, "--json", *args]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": {"message": f"veil-browser timed out after {self.timeout}s"}}
        try:
            return json.loads(r.stdout) if r.stdout.strip() else {"ok": False, "error": {"message": r.stderr.strip() or "empty output"}}
        except json.JSONDecodeError:
            return {"ok": False, "error": {"message": f"unparsable CLI output: {r.stdout[:200]}"}}

    def _current_host(self) -> str | None:
        """Current tab host, from the CLI's local state (no network).
        Does NOT start a daemon: if the session socket doesn't exist
        yet, there is no page open — return None instead of launching
        Chrome just to ask. This preserves the guarantee that a
        denied action never starts a browser."""
        sock = (_cli_base_dir() / "sessions"
                / self.cli_session / "daemon.sock")
        if not sock.exists():
            return None
        r = self._run_cli(["url"])
        if not r.get("ok"):
            return None
        url = (r.get("data") or {}).get("url") or ""
        host = urlparse(url).hostname
        return host

    @staticmethod
    def _host_of(url: str) -> str:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"only http/https URLs are allowed, got scheme {parsed.scheme!r}")
        if not parsed.hostname:
            raise ValueError(f"no host in URL: {url!r}")
        return parsed.hostname

    # -- navigation (needs confirmation) --------------------------------
    def open(self, url: str, approval_token: str | None = None):
        """Open a URL. Domain is broker-checked BEFORE Chrome launches."""
        try:
            host = self._host_of(url)
        except ValueError as e:
            return BrowserDecision(False, "deny", str(e)), None
        d = self._propose("browser.navigate", url, f"open {url} in the task browser",
                          "browser.navigate", [host], approval_token=approval_token)
        if not d.allowed:
            return d, None
        return d, self._run_cli(["open", url])

    def new_tab(self, url: str, approval_token: str | None = None):
        try:
            host = self._host_of(url)
        except ValueError as e:
            return BrowserDecision(False, "deny", str(e)), None
        d = self._propose("browser.navigate", url, f"open {url} in a new tab",
                          "browser.navigate", [host], approval_token=approval_token)
        if not d.allowed:
            return d, None
        return d, self._run_cli(["new-tab", url])

    def back(self, approval_token: str | None = None):
        return self._navigate_local("back", approval_token)

    def forward(self, approval_token: str | None = None):
        return self._navigate_local("forward", approval_token)

    def reload(self, approval_token: str | None = None):
        return self._navigate_local("reload", approval_token)

    def _navigate_local(self, cmd: str, approval_token: str | None = None):
        host = self._current_host()
        if not host:
            return BrowserDecision(False, "deny", "no page open in this session"), None
        d = self._propose("browser.navigate", f"{cmd} on {host}",
                          f"{cmd} in the task browser", "browser.navigate", [host],
                          approval_token=approval_token)
        if not d.allowed:
            return d, None
        return d, self._run_cli([cmd])

    # -- reading (auto if the domain is allowed) -------------------------
    def read(self, max_chars: int = 8000):
        host = self._current_host()
        if not host:
            return BrowserDecision(False, "deny", "no page open in this session"), None
        d = self._propose("browser.read", f"read {host}", f"read visible text of {host}",
                          "browser.read", [host], risk="low")
        if not d.allowed:
            return d, None
        return d, self._run_cli(["read", "--max-chars", str(max_chars)])

    def snapshot(self, interactive: bool = False):
        host = self._current_host()
        if not host:
            return BrowserDecision(False, "deny", "no page open in this session"), None
        d = self._propose("browser.read", f"snapshot {host}",
                          f"accessibility-tree snapshot of {host}",
                          "browser.read", [host], risk="low")
        if not d.allowed:
            return d, None
        args = ["snapshot", "--interactive"] if interactive else ["snapshot"]
        return d, self._run_cli(args)

    def url(self):
        # Local state only — no network, no broker call.
        return self._run_cli(["url"])

    def title(self):
        host = self._current_host()
        if not host:
            return BrowserDecision(False, "deny", "no page open in this session"), None
        d = self._propose("browser.read", f"title of {host}", f"read title of {host}",
                          "browser.read", [host], risk="low")
        if not d.allowed:
            return d, None
        return d, self._run_cli(["title"])

    def tabs(self):
        return self._run_cli(["tabs"])

    def screenshot(self, filename: str = "screenshot.png"):
        """Screenshot is a read, but the file must land in the workspace."""
        if self.workspace is None:
            return BrowserDecision(False, "deny", "no workspace — screenshots need a workspace"), None
        dest = (self.workspace / filename).resolve()
        if not str(dest).startswith(str(self.workspace)):
            return BrowserDecision(False, "deny", "screenshot path must be inside the workspace"), None
        host = self._current_host()
        if not host:
            return BrowserDecision(False, "deny", "no page open in this session"), None
        d = self._propose("browser.screenshot", f"screenshot {host}",
                          f"save a screenshot of {host} to {dest}",
                          "browser.screenshot", [host], risk="low")
        if not d.allowed:
            return d, None
        return d, self._run_cli(["screenshot", str(dest)])

    # -- acting (needs confirmation — login/upload/publish/send live here)
    def click(self, ref: str, approval_token: str | None = None):
        return self._act("click", ref, f"click {ref}", approval_token)

    def fill(self, ref: str, text: str, approval_token: str | None = None):
        return self._act("fill", ref, f"fill {ref} with text", approval_token, extra=[text])

    def type(self, ref: str, text: str, approval_token: str | None = None):
        return self._act("type", ref, f"type text into {ref}", approval_token, extra=[text])

    def press(self, key: str, approval_token: str | None = None):
        return self._act("press", key, f"press {key}", approval_token, extra=[key])

    def scroll(self, direction: str):
        # Scrolling is a view change, not a page action — read class,
        # auto-allowed if the domain is allowed, like snapshot/read.
        host = self._current_host()
        if not host:
            return BrowserDecision(False, "deny", "no page open in this session"), None
        d = self._propose("browser.read", f"scroll {direction} on {host}",
                          f"scroll {direction} on {host}",
                          "browser.read", [host], risk="low")
        if not d.allowed:
            return d, None
        return d, self._run_cli(["scroll", direction])

    def _act(self, cmd: str, target: str, effect: str,
             approval_token: str | None = None, extra: list[str] | None = None):
        host = self._current_host()
        if not host:
            return BrowserDecision(False, "deny", "no page open in this session"), None
        d = self._propose("browser.act", f"{cmd} {target} on {host}", effect,
                          "browser.act", [host], approval_token=approval_token)
        if not d.allowed:
            return d, None
        # CLI forms: click @e1 | fill @e2 "text" | type @e2 "text" | press Enter
        if cmd == "press":
            return d, self._run_cli([cmd, target])
        return d, self._run_cli([cmd, target, *(extra or [])])

    def wait(self, ms: int = 1000):
        # Local wait — no network, no broker call.
        return self._run_cli(["wait", str(ms)])

    # -- lifecycle (local, owning task only) ------------------------------
    def status(self):
        return self._run_cli(["status"])

    def stop(self):
        return self._run_cli(["stop"])
