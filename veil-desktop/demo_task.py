#!/usr/bin/env python3
"""
Demo task for the Security Center — runs a real Developer task
through the broker service + sandbox, and WAITS for the user
in the Security Center when it needs approval.

Steps:
  1. Write main.py + test_main.py in the sandbox (broker-approved,
     reversible-write, auto-allowed).
  2. Ask to open github.com (browser.navigate — external side effect,
     needs user confirmation). Waits up to 60s, re-proposing every 2s.
     Approve in the Security Center -> continues. Deny -> stops
     with "denied by user" (the broker returns deny promptly).
  3. Run the tests in the sandbox.
  4. Try to install a package (install.host-software — denied by
     policy, never executes).
  5. Done.

State is written to run/task.json after every step so the
Security Center UI can show it. SIGTERM (Kill in the UI) is
caught and recorded as "killed".
"""
from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
RUN_DIR = ROOT / "run"
sys.path.insert(0, str(ROOT / "veil-core"))
sys.path.insert(0, str(ROOT / "veil-sandbox"))

from broker_client import BrokerClient, BrokerUnavailable  # noqa: E402
from sandbox import VeilSandbox  # noqa: E402

TASK_FILE = RUN_DIR / "task.json"
WS = ROOT / "demo-workspace"
SESSION = "demo-task-1"

_state = {"name": "Demo task — Developer environment",
          "objective": "Create a small project, write code, run tests, show result.",
          "status": "running", "step": "starting", "updated_at": time.time()}


def set_state(status=None, step=None):
    if status:
        _state["status"] = status
    if step:
        _state["step"] = step
    _state["updated_at"] = time.time()
    TASK_FILE.write_text(json.dumps(_state))


def _on_term(*_):
    set_state("killed", "killed by user in Security Center (SIGTERM)")
    sys.exit(0)


def wait_for_approval(client: BrokerClient, action, target, effect, capability):
    """Propose, wait for the user in the Security Center, re-proposing.
    Returns (outcome, proposal). Deny is prompt (broker remembers
    denials for 5 min); approval is picked up on the next retry."""
    for i in range(30):  # 60s max
        r = client.propose(action=action, target=target, expected_effect=effect,
                           capability=capability)
        if r["outcome"] == "allow":
            return "allow", r
        if r["outcome"] == "deny":
            return "deny", r
        set_state("waiting", f"waiting for approval: {action} {target} (attempt {i+1}/30)")
        time.sleep(2)
    return "timeout", None


def main():
    signal.signal(signal.SIGTERM, _on_term)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return _run()
    except Exception as e:
        # Never crash with a stale "running" state — record the failure.
        set_state("failed", f"task crashed: {type(e).__name__}: {e}")
        return 1


def _run():
    set_state("running", "starting — broker + sandbox")
    import shutil
    if shutil.which("bwrap") is None:
        set_state("failed", "bwrap is not installed on this host (apt install bubblewrap — "
                             "system packages don't survive VM replacement)")
        return 1
    client = BrokerClient(run_dir=RUN_DIR)
    try:
        client.ping()
    except BrokerUnavailable as e:
        set_state("failed", f"broker unavailable: {e}")
        return 1

    sb = VeilSandbox(WS, broker_mode="service", session=SESSION)

    set_state("running", "writing main.py in sandbox")
    d, r, snap = sb.run("cat > main.py <<'EOF'\n\ndef add(a, b):\n    return a + b\n\n\ndef greet(name):\n    return f'hello, {name}'\nEOF")
    if not d.allowed or (r and r.returncode != 0):
        set_state("failed", f"write main.py failed: {d.reason}")
        return 1

    set_state("running", "writing test_main.py in sandbox")
    d, r, snap = sb.run("cat > test_main.py <<'EOF'\nfrom main import add, greet\n\nassert add(2, 3) == 5\nassert add(-1, 1) == 0\nassert greet('veil') == 'hello, veil'\nprint('ALL TESTS PASSED')\nEOF")
    if not d.allowed or (r and r.returncode != 0):
        set_state("failed", f"write test_main.py failed: {d.reason}")
        return 1

    set_state("waiting", "asking to open github.com — approve in Security Center")
    outcome, prop = wait_for_approval(
        client, "browser.navigate", "https://github.com",
        "open github.com in the task browser",
        {"capability": "browser.navigate", "who": f"agent:{SESSION}",
         "scope": {"domains": ["github.com"]}, "session": SESSION, "risk": "medium"})
    if outcome != "allow":
        set_state("denied" if outcome == "deny" else "failed",
                  f"github.com was {outcome} — task stopped here (approval not granted)")
        return 1
    set_state("running", "github.com approved — continuing")

    set_state("running", "running tests in sandbox")
    d, r, snap = sb.run("python3 test_main.py")
    if not d.allowed or (r and r.returncode != 0):
        set_state("failed", f"tests failed: {r.stdout if r else ''} {r.stderr if r else ''}")
        return 1

    set_state("running", "trying to install a package (should be denied by policy)")
    r = client.propose(action="install.host-software", target="pip install pytest",
                       expected_effect="install pytest on host",
                       capability={"capability": "install.host-software",
                                   "who": f"agent:{SESSION}", "scope": {},
                                   "session": SESSION, "risk": "critical"})
    if r["outcome"] == "deny":
        set_state("running", "install denied by policy, as expected — finishing")
    else:
        set_state("running", f"install outcome was {r['outcome']} (expected deny)")

    set_state("done", "ALL TESTS PASSED — main.py + test_main.py written, github.com approved, install denied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
