"""Combined demo: broker decides, sandbox executes. Run: python3 demo.py"""
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from sandbox import VeilSandbox

if __name__ == "__main__":
    sb = VeilSandbox(HERE / "test-workspace")
    print("VEIL — broker + Bubblewrap sandbox, combined")
    print("Broker policy: ls/cat/python3 approved · curl blocked · network broker-only")
    print("Sandbox: /workspace only · /usr read-only · no network · synthetic /etc")
    print("-" * 70)
    for cmd in [
        "ls /workspace",
        "cat notes.md",
        "ls /home/hatch",
        "cat /etc/shadow",
        "curl https://example.com",
        "echo hello > /workspace/hello.txt && cat /workspace/hello.txt",
    ]:
        d, r, snap = sb.run(cmd, snapshot_first=False)
        icon = "✅" if d.allowed else "❌"
        out = (r.stdout.strip().splitlines() or [""]) if r else ["(never executed)"]
        print(f"{icon} {cmd} → {d.reason} | exit={r.returncode if r else '-'} | {out[0][:80]}")
    print("-" * 70)
    print("Full bypass suite: python3 test_sandbox.py  (12 tests)")
