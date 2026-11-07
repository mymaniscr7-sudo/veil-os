"""
VEIL MVP — Developer environment, one complete task (research v2)

Task: "Create a small project, write the requested code, run the
tests, and show me the result."

VEIL then:
  1. Creates a fresh workspace snapshot.
  2. Starts an isolated executor (Bubblewrap sandbox).
  3. Grants the AI only workspace read/write + approved tools.
     No network. No host files.
  4. Uses semantic tools first (broker_v2 proposals, not clicks).
  5. Requires confirmation for network access / package install.
  6. Runs tests with time limits inside the sandbox.
  7. Shows every action in a Security Center report.
  8. Offers rollback / preserves the workspace as a named artifact.

Run: python3 mvp.py
Outputs: mvp-workspace/ (the project), SECURITY-CENTER.md (the report),
  veil-core/audit-v2.log.jsonl (the journal).
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "veil-core"))
sys.path.insert(0, str(HERE / "veil-sandbox"))

from broker_v2 import BrokerV2
from capability import Capability
from sandbox import VeilSandbox

WS = HERE / "mvp-workspace"
REPORT = HERE / "SECURITY-CENTER.md"


def cap(name, scope, risk="medium", session="task-mvp-1"):
    return Capability(capability=name, who="agent:task-mvp-1",
                      scope=scope, session=session, risk=risk)


def main():
    print("VEIL MVP — Developer environment")
    print("Task: create a small project, write code, run tests, show result")
    print("-" * 70)
    # 1. Fresh workspace + snapshot
    if WS.exists():
        import shutil
        shutil.rmtree(WS)
    WS.mkdir(parents=True)
    broker = BrokerV2(HERE / "veil-core" / "policy_v2.example.json")
    sb = VeilSandbox(WS)
    snap = sb.snapshot()
    print(f"1. Fresh workspace {WS.name}, snapshot {snap}")

    # 2-3. Isolated executor, workspace-only capabilities, no network
    print("2. Isolated executor: Bubblewrap, /workspace only, no network")
    print("3. Capabilities granted: filesystem.read/write in /workspace only,")
    print("   terminal.run (approved commands only). Network: approved-domains,")
    print("   but the sandbox has no route — defence in depth.")

    # 4. Semantic tools: propose -> validate -> execute
    files = {
        "main.py": '"""MVP project — written by the agent, via the broker."""\n\n\ndef add(a, b):\n    """Return a + b."""\n    return a + b\n',
        "test_main.py": '"""Tests for main.py — run inside the sandbox."""\nfrom main import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n    assert add(-1, 1) == 0\n    assert add(0, 0) == 0\n    print("test_add: PASS")\n\n\nif __name__ == "__main__":\n    test_add()\n    print("ALL TESTS PASSED")\n',
    }
    for name, content in files.items():
        p = broker.propose(action="filesystem.write", target=f"/workspace/{name}",
                           expected_effect=f"create one file {name}",
                           rollback="snapshot-required",
                           capability=cap("filesystem.write", {"paths": [f"/workspace/{name}"]}))
        print(f"4. propose write {name} -> {p.outcome}: {p.reason}")
        assert p.outcome == "allow", p.reason
        (WS / name).write_text(content)

    # 5. Network / install require confirmation — and are denied here
    p = broker.propose(action="browser.navigate", target="https://pypi.org",
                       expected_effect="open pypi to install a package",
                       rollback="none",
                       capability=cap("browser.navigate", {"domains": ["pypi.org"]}))
    print(f"5. propose network pypi.org -> {p.outcome}: {p.reason} (no approval given)")
    p2 = broker.propose(action="install.host-software", target="pytest",
                        expected_effect="install pytest on host",
                        rollback="none",
                        capability=cap("install.host-software", {"package": "pytest"}, risk="critical"))
    print(f"   propose install pytest -> {p2.outcome}: {p2.reason}")

    # 6. Run tests inside the sandbox, with a time limit
    print("6. Running tests inside the sandbox (timeout 30s)...")
    decision, result, _ = sb.run("python3 test_main.py", timeout=30, snapshot_first=False)
    print(f"   broker: {decision.reason} | exit={result.returncode if result else '-'}")
    if result:
        print("   " + result.stdout.replace("\n", "\n   ").strip())
        if result.stderr:
            print("   stderr:", result.stderr.strip()[:200])
    assert result and result.returncode == 0 and "ALL TESTS PASSED" in result.stdout

    # 7. Security Center report
    report = f"""# VEIL Security Center — MVP task report

Task: task-mvp-1 — "Create a small project, write code, run tests, show result"
Environment: Developer · Executor: Bubblewrap sandbox · Network: approved-domains (sandbox: no route)
Snapshot: {snap} · Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}

## Active capabilities (all expire in 15 min, bound to task-mvp-1)
- filesystem.read/write — scope /workspace/** — risk medium — auto (reversible, snapshot)
- terminal.run — approved commands only — risk medium
- browser.navigate — github.com, pypi.org — risk medium — confirmation required
- api.call, system.settings, install.host-software, camera.access — DENIED

## Actions (from veil-core/audit-v2.log.jsonl)
- filesystem.write /workspace/main.py — allow
- filesystem.write /workspace/test_main.py — allow
- browser.navigate https://pypi.org — needs_user_confirmation (no approval given)
- install.host-software pytest — deny (policy)
- terminal.run python3 test_main.py — allow, exit 0, ALL TESTS PASSED

## Controls
- Pause AI / Kill task: stops the executor (bwrap --die-with-parent), not the model
- Rollback: restore snapshot {snap} — `VeilSandbox(WS).rollback('{snap}')`
- Preserve as artifact: copy mvp-workspace/ to a named artifact

## Result
Project created, tests passed inside the sandbox, no network, no host access.
"""
    REPORT.write_text(report)
    print("7. Security Center report written to SECURITY-CENTER.md")
    print("-" * 70)
    print("MVP PASSED — project in mvp-workspace/, tests passed in sandbox.")
    print(f"Rollback snapshot: {snap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
