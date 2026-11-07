#!/usr/bin/env python3
"""
Researcher demo task — the AI browses with veil-browser-cli, not a GUI.

Steps:
  1. Open github.com through the broker-gated browser (needs user
     confirmation in the Security Center; waits up to 60s).
  2. Snapshot the accessibility tree (@eN refs) and read the page text.
  3. Save findings.md in the task workspace (via the sandbox, so the
     write is broker-approved and snapshotted).
  4. Done.

The browser is a CLI: `snapshot` to see, `@e1` to act. No screenshots,
no HTML soup, no pixel clicking. One CLI session per task, own Chrome
profile, no host cookies. State in run/task.json, SIGTERM caught.
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

from browser_tool import VeilBrowser  # noqa: E402
from sandbox import VeilSandbox  # noqa: E402

TASK_FILE = RUN_DIR / "task.json"
WS = ROOT / "research-workspace"
SESSION = "researcher-1"

_state = {"name": "Researcher task — browse with the CLI browser",
          "objective": "Open github.com, read it via the accessibility tree, save findings.",
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


def main():
    signal.signal(signal.SIGTERM, _on_term)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return _run()
    except Exception as e:
        set_state("failed", f"task crashed: {type(e).__name__}: {e}")
        return 1


def _run():
    set_state("running", "starting — broker-gated browser")
    b = VeilBrowser(session=SESSION, workspace=WS, broker_run_dir=RUN_DIR)
    sb = VeilSandbox(WS, broker_mode="service", session=SESSION)

    # 1. Open — needs confirmation. Retry until approved/denied/timeout.
    set_state("waiting", "asking to open github.com — approve in Security Center")
    outcome = None
    for i in range(30):
        d, r = b.open("https://github.com")
        if d.allowed:
            outcome = "allow"
            break
        if d.outcome == "deny":
            outcome = "deny"
            break
        set_state("waiting", f"waiting for approval: open github.com (attempt {i+1}/30)")
        time.sleep(2)
    if outcome != "allow":
        set_state("denied" if outcome == "deny" else "failed",
                  f"github.com was {outcome} — task stopped here")
        return 1
    set_state("running", "github.com approved — reading via accessibility tree")

    # 2. Snapshot + read (auto — read class).
    d, snap = b.snapshot(interactive=True)
    if not d.allowed:
        set_state("failed", f"snapshot denied: {d.reason}")
        return 1
    d, read = b.read(max_chars=2000)
    if not d.allowed:
        set_state("failed", f"read denied: {d.reason}")
        return 1
    d, title = b.title()
    page_title = ((title or {}).get("data") or {}).get("title", "unknown")
    snap_text = (snap or {}).get("text", "")[:1500].replace("'", "")
    read_text = (read or {}).get("text", "")[:1500].replace("'", "").replace("\n", " ")

    # 3. Save findings via the sandbox (broker-approved write).
    set_state("running", "saving findings.md in workspace")
    findings = (
        f"# Findings — github.com\n\n"
        f"Title: {page_title}\n\n"
        f"## Snapshot (interactive elements)\n\n```\n{snap_text}\n```\n\n"
        f"## Read (first 1500 chars)\n\n{read_text}\n"
    )
    import base64
    b64 = base64.b64encode(findings.encode()).decode()
    d, r, _ = sb.run(f"python3 -c \"import base64; open('/workspace/findings.md','w').write(base64.b64decode('{b64}').decode())\"")
    if not d.allowed or (r and r.returncode != 0):
        detail = f"allowed={d.allowed} reason={d.reason}"
        if r is not None:
            detail += f" exit={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}"
        set_state("failed", f"write findings.md failed: {detail}")
        return 1

    b.stop()
    set_state("done", f"findings.md saved — title: {page_title}, snapshot + read via CLI browser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
