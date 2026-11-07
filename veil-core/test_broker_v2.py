"""Broker v2 tests. Run: python3 test_broker_v2.py  (from veil-core/)
Exit 0 = all pass.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from broker_v2 import BrokerV2
from capability import Capability

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ PASS {name}")
    else:
        failed += 1
        print(f"❌ FAIL {name} — {detail}")


def cap(name, scope=None, risk="medium", session="task-8f21", **kw):
    return Capability(capability=name, who="agent:task-8f21",
                      scope=scope or {}, session=session, risk=risk, **kw)


def main():
    journal = HERE / "audit-v2-test.log.jsonl"
    if journal.exists():
        journal.unlink()
    b = BrokerV2(HERE / "policy_v2.example.json", journal)

    # 1. Reversible write inside workspace: allow
    p = b.propose(action="filesystem.write", target="/workspace/main.py",
                  expected_effect="create one Python file", rollback="snapshot-required",
                  capability=cap("filesystem.write", {"paths": ["/workspace/**"]}))
    check("filesystem.write allowed", p.outcome == "allow", p.reason)

    # 2. API blocked by policy
    p = b.propose(action="api.call", target="some-api",
                  expected_effect="call external API", rollback="none",
                  capability=cap("api.call", {"provider": "some-api"}))
    check("api.call denied by policy", p.outcome == "deny", p.reason)

    # 3. Network approved-domains: github.com allowed, evil.com denied
    p = b.propose(action="browser.navigate", target="https://github.com",
                  expected_effect="open github", rollback="none",
                  capability=cap("browser.navigate", {"domains": ["github.com"]}))
    # browser.navigate is external-side-effect -> needs confirmation without token
    check("github needs confirmation (not auto-allow)", p.outcome == "needs_user_confirmation", p.reason)
    c = cap("browser.navigate", {"domains": ["github.com"]})
    token = b.grant_approval(c)
    p = b.propose(action="browser.navigate", target="https://github.com",
                  expected_effect="open github", rollback="none",
                  capability=c, approval_token=token)
    check("github allowed with exact-action approval", p.outcome == "allow", p.reason)
    p = b.propose(action="browser.navigate", target="https://evil.com",
                  expected_effect="open evil", rollback="none",
                  capability=cap("browser.navigate", {"domains": ["evil.com"]}))
    check("evil.com denied in approved-domains state", p.outcome == "deny", p.reason)

    # 4. Approval binds to EXACT action — token for github doesn't work for pypi
    c2 = cap("browser.navigate", {"domains": ["pypi.org"]})
    p = b.propose(action="browser.navigate", target="https://pypi.org",
                  expected_effect="open pypi", rollback="none",
                  capability=c2, approval_token=token)
    check("approval does not transfer to a different action", p.outcome == "needs_user_confirmation", p.reason)

    # 5. Critical risk needs step-up, even with a normal approval.
    # Use filesystem.write (policy-allowed) with critical risk, so the
    # step-up path is reached — install.host-software is policy-denied
    # outright, and deny is the correct outcome for that one.
    c3 = cap("filesystem.write", {"paths": ["/workspace/critical.txt"]}, risk="critical")
    token3 = b.grant_approval(c3)
    p = b.propose(action="filesystem.write", target="/workspace/critical.txt",
                  expected_effect="write critical file", rollback="snapshot-required",
                  capability=c3, approval_token=token3)
    check("critical action allowed only with step-up approval", p.outcome == "allow", p.reason)
    p = b.propose(action="filesystem.write", target="/workspace/critical.txt",
                  expected_effect="write critical file", rollback="snapshot-required",
                  capability=cap("filesystem.write", {"paths": ["/workspace/critical2.txt"]}, risk="critical", session="task-critical-fresh"))
    check("critical action without approval needs step-up", p.outcome == "needs_step_up_authentication", p.reason)
    p = b.propose(action="install.host-software", target="x",
                  expected_effect="install host software", rollback="none",
                  capability=cap("install.host-software", {"package": "x"}, risk="critical"))
    check("policy-denied install is deny, not step-up", p.outcome == "deny", p.reason)

    # 6. Expired capability denies (fail-closed on expiry)
    expired = cap("filesystem.write", {"paths": ["/workspace/**"]},
                  expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    p = b.propose(action="filesystem.write", target="/workspace/x.py",
                  expected_effect="write", rollback="snapshot-required", capability=expired)
    check("expired capability denied", p.outcome == "deny", p.reason)

    # 7. Fail-closed: missing policy
    try:
        BrokerV2(HERE / "no-such-policy.json", journal)
        check("missing policy fails closed at startup", False, "broker started without policy")
    except ValueError:
        check("missing policy fails closed at startup", True)

    # 8. Fail-closed: journal unwritable -> propose denies
    b_bad = BrokerV2(HERE / "policy_v2.example.json", HERE)  # journal path is a dir
    p = b_bad.propose(action="filesystem.write", target="/workspace/x.py",
                      expected_effect="write", rollback="snapshot-required",
                      capability=cap("filesystem.write", {"paths": ["/workspace/**"]}))
    check("journal failure fails closed to deny", p.outcome == "deny", p.reason)

    # 9. Model-generated user_confirmed is never trusted (no token = no allow).
    # Use a fresh session so no earlier stored approval for github.com applies.
    p = b.propose(action="browser.navigate", target="https://github.com",
                  expected_effect="open github", rollback="none",
                  capability=cap("browser.navigate", {"domains": ["github.com"]}, session="task-9-fresh"))
    check("no token, no allow — model cannot self-approve", p.outcome != "allow", p.outcome)

    # 10. Stored approval: grant via admin, then retry WITHOUT a token -> allow.
    # The broker holds the approval; the agent retrying the exact action
    # doesn't need to present the token again. A wrong token still fails.
    c10 = cap("browser.navigate", {"domains": ["github.com"]}, session="task-10")
    b.grant_approval(c10)
    p = b.propose(action="browser.navigate", target="https://github.com",
                  expected_effect="open github", rollback="none", capability=c10)
    check("stored approval allows retry without token", p.outcome == "allow", p.reason)
    p = b.propose(action="browser.navigate", target="https://github.com",
                  expected_effect="open github", rollback="none", capability=c10,
                  approval_token="appr-wrongtoken")
    check("wrong token does not allow", p.outcome == "needs_user_confirmation", p.reason)

    # 11. Deny in Security Center -> re-propose returns deny promptly, not needs_*.
    c11 = cap("browser.navigate", {"domains": ["pypi.org"]}, session="task-11")
    p = b.propose(action="browser.navigate", target="https://pypi.org",
                  expected_effect="open pypi", rollback="none", capability=c11)
    check("pypi first propose needs confirmation", p.outcome == "needs_user_confirmation", p.reason)
    check("deny_pending works", b.deny_pending(p.proposal_id) is True)
    p = b.propose(action="browser.navigate", target="https://pypi.org",
                  expected_effect="open pypi", rollback="none", capability=c11)
    check("denied action re-proposed returns deny", p.outcome == "deny", p.reason)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
