"""VEIL Sandbox bypass tests. Run: python3 test_sandbox.py
Each test asserts the sandbox/broker actually blocks what it claims to.
Exit 0 = all pass. No test is allowed to silently pass on error output.
"""
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from sandbox import VeilSandbox

WS = HERE / "test-workspace"
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ PASS {name}")
    else:
        failed += 1
        print(f"❌ FAIL {name} — {detail}")


def run(cmd):
    sb = VeilSandbox(WS, broker_mode="inprocess")
    decision, result, snap = sb.run(cmd, snapshot_first=False)
    return decision, result


def main():
    (WS / "notes.md").write_text("hello from workspace\n")

    # 1. Useful work inside the workspace works
    d, r = run("ls /workspace")
    check("workspace ls works", d.allowed and r and "notes.md" in r.stdout, str(r.stdout if r else d.reason))
    d, r = run("cat notes.md")
    check("workspace cat works", d.allowed and r and "hello from workspace" in r.stdout)

    # 2. Host files are invisible — not just denied, not mounted
    d, r = run("ls /home/hatch")
    check("host home invisible", r is not None and "No such file" in r.stderr + r.stdout, str(r.stdout + r.stderr if r else "no result"))
    d, r = run("cat /home/hatch/MEMORY.md")
    check("host MEMORY.md invisible", r is not None and "No such file" in (r.stdout + r.stderr))

    # 3. /etc is synthetic, no host secrets
    d, r = run("ls /etc")
    check("host /etc not present", r is not None and "shadow" not in r.stdout and "passwd" in r.stdout, str(r.stdout if r else ""))
    d, r = run("cat /etc/shadow")
    check("/etc/shadow invisible", r is not None and "No such file" in (r.stdout + r.stderr))

    # 4. Writes outside workspace fail; inside succeed
    d, r = run("echo x > /usr/evil")
    check("write to /usr fails (read-only)", r is not None and r.returncode != 0, str(r.stderr if r else ""))
    d, r = run("echo ok > /workspace/from-sandbox.txt && cat /workspace/from-sandbox.txt")
    check("write inside workspace works", d.allowed and r and "ok" in r.stdout)
    (WS / "from-sandbox.txt").unlink(missing_ok=True)

    # 5. Network is gone — broker deny AND namespace deny (defence in depth)
    d, r = run("curl https://example.com")
    check("curl blocked by broker (not approved)", not d.allowed, d.reason)
    # Even an approved-looking tool can't phone home if it gets in:
    # python3 is approved in policy, but its socket has no route.
    sb = VeilSandbox(WS, broker_mode="inprocess")
    dec, res, _ = sb.run("python3 -c \"import socket; socket.create_connection(('example.com', 443), timeout=3)\"",
                         snapshot_first=False)
    # python3 is approved, so broker allows; the NAMESPACE must stop it.
    check("python socket has no route (namespace)", res is not None and res.returncode != 0,
          str(res.stdout + res.stderr if res else dec.reason))

    # 6. Broker deny means no execution at all
    d, r = run("curl https://example.com")
    check("denied command never executes", r is None)

    # 7. Snapshot + rollback works
    sb = VeilSandbox(WS, broker_mode="inprocess")
    snap = sb.snapshot()
    (WS / "temp.txt").write_text("temp")
    sb.rollback(snap)
    check("rollback restores workspace", not (WS / "temp.txt").exists())

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
