"""Broker-gated browser tool tests. Run: python3 test_browser_tool.py
Starts a real broker subprocess on a COPY of the policy (network
toggles write to the policy file — never the real one), then drives
VeilBrowser against the live veil-browser-cli + Chrome for Testing.
Exit 0 = all pass.

The key guarantee under test: the domain is broker-checked BEFORE
Chrome ever launches. A denied domain must not create a CLI session
directory at all.
"""
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from broker_client import AdminClient  # noqa: E402
from browser_tool import VeilBrowser, find_cli_binary  # noqa: E402

passed = failed = 0
SESSIONS_DIR = Path.home() / ".cache" / "veil-browser" / "sessions"


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ PASS {name}")
    else:
        failed += 1
        print(f"❌ FAIL {name} — {detail}")


def approve_pending(admin):
    pending = admin.list_pending()["pending"]
    if not pending:
        return None
    return admin.approve_pending(pending[0]["proposal_id"])


def main():
    tmp = Path(tempfile.mkdtemp(prefix="veil-browser-test-"))
    run_dir = tmp / "run"
    run_dir.mkdir()
    ws = tmp / "workspace"
    ws.mkdir()
    # Copy policy: network toggles persist to the file.
    policy = tmp / "policy.json"
    shutil.copy(HERE / "policy_v2.example.json", policy)

    proc = subprocess.Popen(
        [sys.executable, str(HERE / "broker_service.py"), "--serve",
         "--policy", str(policy), "--run-dir", str(run_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            if (run_dir / "veil-broker-agent.sock").exists():
                break
            time.sleep(0.1)
        admin = AdminClient(run_dir=run_dir)
        check("broker up for browser tests", admin.status().get("ok") is True)
        check("veil-browser CLI binary found", find_cli_binary() is not None,
              str(find_cli_binary()))

        tag = f"test-{int(time.time())}"
        b = VeilBrowser(session=f"browser-{tag}", workspace=ws, broker_run_dir=run_dir)
        sess_dir = SESSIONS_DIR / b.cli_session

        # 1. Open github.com without approval -> needs confirmation,
        #    and Chrome NEVER launches (no session dir).
        d, r = b.open("https://github.com")
        check("github.com without approval needs confirmation",
              d.outcome == "needs_user_confirmation", d.reason)
        check("denied-before-launch: no CLI session dir created",
              not sess_dir.exists(), str(sess_dir))
        check("no result when not allowed", r is None)

        # 2. Approve in Security Center -> open works for real.
        tok = approve_pending(admin)
        check("admin approve returns a token", tok is not None and "token" in tok, str(tok))
        d, r = b.open("https://github.com")
        check("github.com opens after approval", d.allowed and r is not None and r.get("ok"),
              f"{d} {r}")
        check("session dir now exists (Chrome launched only after approval)",
              sess_dir.exists())
        d, r = b.title()
        title = ((r or {}).get("data") or {}).get("title", "")
        check("title is GitHub", "GitHub" in title, title)

        # 3. Read / snapshot are auto (read class) once the domain is allowed.
        # Note: read text lives in the top-level "text" field, not data.text.
        d, r = b.read(max_chars=2000)
        text = (r or {}).get("text", "")
        check("read returns page text", d.allowed and len(text) > 100,
              f"len={len(text)}")
        d, r = b.snapshot(interactive=True)
        check("snapshot returns @eN refs", d.allowed and r is not None and r.get("ok")
              and "@e" in (r.get("text") or ""), str(r)[:200])

        # 4. Scroll is a read — auto-allowed.
        d, r = b.scroll("down")
        check("scroll is auto-allowed (read class)", d.allowed and r is not None and r.get("ok"),
              f"{d} {r}")

        # 5. Click without approval -> needs confirmation, URL unchanged.
        before = b.url()
        d, r = b.click("@e1")
        check("click without approval needs confirmation",
              d.outcome == "needs_user_confirmation", d.reason)
        after = b.url()
        check("click denied: URL unchanged",
              (before.get("data") or {}) == (after.get("data") or {}),
              f"{before} vs {after}")

        # 6. Screenshot lands in the workspace only.
        d, r = b.screenshot("page.png")
        shot = ws / "page.png"
        check("screenshot saved in workspace", d.allowed and shot.exists() and shot.stat().st_size > 0,
              f"{d} exists={shot.exists()}")

        # 7. Non-allowlisted domain -> deny, Chrome never launches
        #    for that session.
        b2 = VeilBrowser(session=f"browser-evil-{tag}", workspace=ws, broker_run_dir=run_dir)
        d, r = b2.open("https://evil.example.com")
        check("evil.example.com denied by policy", d.outcome == "deny", d.reason)
        check("denied domain: no CLI session dir for that task",
              not (SESSIONS_DIR / b2.cli_session).exists())

        # 8. Network offline -> deny before launch.
        admin.set_network_state("offline")
        b3 = VeilBrowser(session=f"browser-off-{tag}", workspace=ws, broker_run_dir=run_dir)
        d, r = b3.open("https://github.com")
        check("offline: github.com denied", d.outcome == "deny", d.reason)
        check("offline: no CLI session dir", not (SESSIONS_DIR / b3.cli_session).exists())
        admin.set_network_state("approved-domains")

        # 9. file:// URLs are rejected locally, no broker call.
        d, r = b.open("file:///etc/passwd")
        check("file:// URL rejected", d.outcome == "deny" and "http" in d.reason, d.reason)

        # 10. Stop cleans up the session.
        r = b.stop()
        check("stop works", r.get("ok") is True, str(r))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        # Best-effort cleanup of test sessions.
        for s in [f"browser-{tag}", f"browser-evil-{tag}", f"browser-off-{tag}"]:
            try:
                subprocess.run([str(find_cli_binary()), "--session", f"veil-{s}", "stop"],
                               capture_output=True, timeout=10)
            except Exception:
                pass

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
