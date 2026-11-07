"""
VEIL MVP via the separate broker process.

Same task as mvp.py, but nothing in the agent imports the broker:
  - broker runs as its own process (veil-broker.py start)
  - agent talks to it only via BrokerClient (agent socket)
  - approvals come from AdminClient (admin socket, 0600)
  - sandbox runs commands only after a broker allow via RPC
  - kill the broker mid-task and the task fails closed, nothing executes

Run: python3 mvp_service.py
It starts the broker, runs the MVP, stops the broker, and leaves
mvp-workspace/ + SECURITY-CENTER-SERVICE.md behind.
"""
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "veil-core"))
sys.path.insert(0, str(HERE / "veil-sandbox"))

from broker_client import AdminClient, BrokerClient  # noqa: E402
from sandbox import VeilSandbox  # noqa: E402

WS = HERE / "mvp-workspace-service"
REPORT = HERE / "SECURITY-CENTER-SERVICE.md"
RUN_DIR = HERE / "run"


def cap(name, scope, session="task-mvp-service", risk="medium"):
    return {"capability": name, "who": f"agent:{session}",
            "scope": scope, "session": session, "risk": risk}


def main():
    print("VEIL MVP — via separate broker process")
    print("-" * 70)
    # Start broker (idempotent: start prints "already running" if so)
    subprocess.run([sys.executable, str(HERE / "veil-core" / "veil-broker.py"),
                    "start"], check=False)
    try:
        client = BrokerClient(run_dir=RUN_DIR)
        admin = AdminClient(run_dir=RUN_DIR)
        print("broker status:", admin.status())

        if WS.exists():
            shutil.rmtree(WS)
        WS.mkdir(parents=True)
        sb = VeilSandbox(WS, broker_run_dir=RUN_DIR, session="task-mvp-service")
        snap = sb.snapshot()
        print(f"1. Fresh workspace, snapshot {snap}")

        # Semantic proposals via RPC — agent never imports the broker.
        files = {
            "main.py": 'def add(a, b):\n    return a + b\n',
            "test_main.py": (
                'from main import add\n\n\ndef test_add():\n'
                '    assert add(2, 3) == 5\n    assert add(-1, 1) == 0\n    print("test_add: PASS")\n\n\n'
                'if __name__ == "__main__":\n    test_add()\n    print("ALL TESTS PASSED")\n'),
        }
        for name, content in files.items():
            r = client.propose(action="filesystem.write", target=f"/workspace/{name}",
                               expected_effect=f"create one file {name}",
                               capability=cap("filesystem.write", {"paths": [f"/workspace/{name}"]}))
            print(f"2. propose write {name} -> {r['outcome']}: {r['reason']}")
            assert r["outcome"] == "allow", r
            (WS / name).write_text(content)

        # Network needs confirmation; grant via ADMIN socket, then it allows.
        r = client.propose(action="browser.navigate", target="https://github.com",
                           expected_effect="open github",
                           capability=cap("browser.navigate", {"domains": ["github.com"]}))
        print(f"3. propose github (no token) -> {r['outcome']}")
        assert r["outcome"] == "needs_user_confirmation"
        token = admin.grant_approval(cap("browser.navigate", {"domains": ["github.com"]}))["token"]
        r = client.propose(action="browser.navigate", target="https://github.com",
                           expected_effect="open github",
                           capability=cap("browser.navigate", {"domains": ["github.com"]}),
                           approval_token=token)
        print(f"   with admin token -> {r['outcome']}: {r['reason']}")
        assert r["outcome"] == "allow"

        # Run tests inside the sandbox, broker-gated via RPC.
        print("4. Running tests inside the sandbox via service-gated sandbox...")
        decision, result, _ = sb.run("python3 test_main.py", timeout=30, snapshot_first=False)
        print(f"   broker: {decision.reason} | exit={result.returncode if result else '-'}")
        if result:
            print("   " + result.stdout.replace("\n", "\n   ").strip())
        assert result and result.returncode == 0 and "ALL TESTS PASSED" in result.stdout

        REPORT.write_text(
            "# VEIL Security Center — MVP via separate broker process\n\n"
            "Task: task-mvp-service — create a small project, write code, run tests\n"
            f"Snapshot: {snap} · Broker: separate process, agent socket 0660, admin socket 0600\n\n"
            "## What changed vs the in-process MVP\n"
            "- Agent never imports BrokerV2, never reads policy_v2.example.json, never touches the journal.\n"
            "- Agent proposes via BrokerClient (agent socket: propose/ping only).\n"
            "- Approval comes from AdminClient (admin socket: grant/revoke/status/shutdown).\n"
            "- Sandbox asks the broker over RPC before every bwrap run; broker down = no execution.\n"
            "- Approvals are in-memory in the broker; a restart clears them.\n\n"
            "## Actions\n"
            "- filesystem.write main.py / test_main.py — allow (via RPC)\n"
            "- browser.navigate github.com — needs_user_confirmation, then allow with admin token\n"
            "- terminal.run python3 test_main.py — allow (via RPC), exit 0, ALL TESTS PASSED\n\n"
            "## Result\n"
            "MVP PASSED via separate broker process.\n")
        print("5. Report written to SECURITY-CENTER-SERVICE.md")
        print("-" * 70)
        print("MVP (service) PASSED.")
        return 0
    finally:
        subprocess.run([sys.executable, str(HERE / "veil-core" / "veil-broker.py"),
                        "stop"], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
