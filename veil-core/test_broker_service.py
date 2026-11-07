"""Broker-as-a-service tests. Run: python3 test_broker_service.py
Starts a real broker subprocess on test sockets, talks to it only
via the RPC clients, then kills it and proves fail-closed.
Exit 0 = all pass.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "veil-sandbox"))

from broker_client import AdminClient, BrokerClient, BrokerUnavailable  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ PASS {name}")
    else:
        failed += 1
        print(f"❌ FAIL {name} — {detail}")


def raw_call(sock_path, obj):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(sock_path))
        s.sendall((json.dumps(obj) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode())


def cap(name, scope=None, session="task-1", risk="medium"):
    return {"capability": name, "who": f"agent:{session}",
            "scope": scope or {}, "session": session, "risk": risk}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="veil-broker-test-"))
    run_dir = tmp / "run"
    run_dir.mkdir()
    policy = HERE / "policy_v2.example.json"

    proc = subprocess.Popen(
        [sys.executable, str(HERE / "broker_service.py"), "--serve",
         "--policy", str(policy), "--run-dir", str(run_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        agent_sock = run_dir / "veil-broker-agent.sock"
        admin_sock = run_dir / "veil-broker-admin.sock"
        for _ in range(50):
            if agent_sock.exists() and admin_sock.exists():
                break
            time.sleep(0.1)
        check("broker starts, both sockets exist",
              agent_sock.exists() and admin_sock.exists())
        # Socket permissions: admin 0600, agent 0660
        check("admin socket is 0600", oct(admin_sock.stat().st_mode & 0o777) == "0o600",
              oct(admin_sock.stat().st_mode & 0o777))
        check("agent socket is 0660", oct(agent_sock.stat().st_mode & 0o777) == "0o660",
              oct(agent_sock.stat().st_mode & 0o777))

        agent = BrokerClient(run_dir=run_dir)
        admin = AdminClient(run_dir=run_dir)

        # 1. Agent propose: filesystem.write -> allow
        r = agent.propose(action="filesystem.write", target="/workspace/x.py",
                          expected_effect="create file",
                          capability=cap("filesystem.write", {"paths": ["/workspace/**"]}))
        check("agent propose filesystem.write -> allow", r["outcome"] == "allow", str(r))

        # 2. Agent propose: api.call -> deny
        r = agent.propose(action="api.call", target="x", expected_effect="call api",
                          capability=cap("api.call", {"provider": "x"}))
        check("agent propose api.call -> deny", r["outcome"] == "deny", str(r))

        # 3. Agent cannot grant approval via agent socket
        resp = raw_call(agent_sock, {"method": "grant_approval",
                                     "params": {"capability": cap("browser.navigate", {"domains": ["github.com"]})},
                                     "id": "t1"})
        check("agent socket rejects grant_approval", "error" in resp, str(resp))

        # 4. Agent cannot read policy / status via agent socket
        resp = raw_call(agent_sock, {"method": "status", "params": {}, "id": "t2"})
        check("agent socket rejects status", "error" in resp, str(resp))
        resp = raw_call(agent_sock, {"method": "shutdown", "params": {}, "id": "t3"})
        check("agent socket rejects shutdown", "error" in resp, str(resp))
        check("broker still alive after agent shutdown attempt",
              agent.ping().get("ok") is True)

        # 5. Browser needs confirmation, admin grant makes it allow, token doesn't transfer
        r = agent.propose(action="browser.navigate", target="https://github.com",
                          expected_effect="open github",
                          capability=cap("browser.navigate", {"domains": ["github.com"]}))
        check("github without token -> needs_user_confirmation",
              r["outcome"] == "needs_user_confirmation", str(r))
        token = admin.grant_approval(cap("browser.navigate", {"domains": ["github.com"]}))["token"]
        r = agent.propose(action="browser.navigate", target="https://github.com",
                          expected_effect="open github",
                          capability=cap("browser.navigate", {"domains": ["github.com"]}),
                          approval_token=token)
        check("github with admin token -> allow", r["outcome"] == "allow", str(r))
        r = agent.propose(action="browser.navigate", target="https://pypi.org",
                          expected_effect="open pypi",
                          capability=cap("browser.navigate", {"domains": ["pypi.org"]}),
                          approval_token=token)
        check("token does not transfer to pypi.org",
              r["outcome"] == "needs_user_confirmation", str(r))

        # 6. Malformed / unknown method: error, broker stays up
        resp = raw_call(agent_sock, {"method": "nope", "params": {}, "id": "t4"})
        check("unknown method -> error", "error" in resp, str(resp))
        check("broker stays up after bad request", agent.ping().get("ok") is True)

        # 7. Two separate client processes share broker state (approval visible)
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); "
             "from broker_client import BrokerClient; "
             "c = BrokerClient(run_dir=%r); "
             "print(c.propose(action='browser.navigate', target='https://github.com', "
             "expected_effect='x', capability={'capability':'browser.navigate','who':'agent:task-1',"
             "'scope':{'domains':['github.com']},'session':'task-1','risk':'medium'}, "
             "approval_token=%r)['outcome'])" % (str(HERE), str(run_dir), token)],
            capture_output=True, text=True)
        check("second process sees admin approval via broker",
              out.stdout.strip() == "allow", out.stdout + out.stderr)

        # 8. Sandbox via service: ls allowed, curl denied
        from sandbox import VeilSandbox
        ws = tmp / "ws"
        ws.mkdir()
        (ws / "notes.md").write_text("hello\n")
        sb = VeilSandbox(ws, broker_run_dir=run_dir, session="task-1")
        d, r2, _ = sb.run("ls /workspace", snapshot_first=False)
        check("sandbox via service: ls allowed", d.allowed and r2 and "notes.md" in r2.stdout,
              f"{d.reason} {r2.stdout if r2 else ''}")
        d, r2, _ = sb.run("curl https://example.com", snapshot_first=False)
        check("sandbox via service: curl denied, never executed",
              not d.allowed and r2 is None, d.reason)

        # 9. Kill broker -> client fails closed, sandbox does NOT execute
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
        check("broker process exits on SIGTERM", proc.returncode is not None)
        try:
            agent.propose(action="filesystem.write", target="/workspace/x.py",
                          expected_effect="x",
                          capability=cap("filesystem.write", {"paths": ["/workspace/**"]}))
            check("client raises BrokerUnavailable when broker is down", False, "no exception")
        except BrokerUnavailable:
            check("client raises BrokerUnavailable when broker is down", True)
        marker = ws / "should-not-exist.txt"
        sb2 = VeilSandbox(ws, broker_run_dir=run_dir, session="task-1")
        d, r2, _ = sb2.run("echo pwned > /workspace/should-not-exist.txt", snapshot_first=False)
        check("sandbox fails closed when broker is down (no execution)",
              not d.allowed and r2 is None and not marker.exists(),
              f"{d.reason} marker={marker.exists()}")

        # 10. Restart broker: approvals do NOT survive (in-memory, intentional)
        proc2 = subprocess.Popen(
            [sys.executable, str(HERE / "broker_service.py"), "--serve",
             "--policy", str(policy), "--run-dir", str(run_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(50):
                if agent_sock.exists():
                    break
                time.sleep(0.1)
            agent2 = BrokerClient(run_dir=run_dir)
            r = agent2.propose(action="browser.navigate", target="https://github.com",
                               expected_effect="open github",
                               capability=cap("browser.navigate", {"domains": ["github.com"]}),
                               approval_token=token)
            check("approvals do not survive broker restart",
                  r["outcome"] == "needs_user_confirmation", str(r))
            r = agent2.propose(action="filesystem.write", target="/workspace/x.py",
                               expected_effect="x",
                               capability=cap("filesystem.write", {"paths": ["/workspace/**"]}))
            check("broker works again after restart", r["outcome"] == "allow", str(r))
        finally:
            proc2.send_signal(signal.SIGTERM)
            try:
                proc2.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc2.kill()
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
