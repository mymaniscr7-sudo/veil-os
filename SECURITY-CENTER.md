# VEIL Security Center — MVP task report

Task: task-mvp-1 — "Create a small project, write code, run tests, show result"
Environment: Developer · Executor: Bubblewrap sandbox · Network: approved-domains (sandbox: no route)
Snapshot: 1793939030-bf6baf3d · Generated: 2026-11-05 23:23:50 EST

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
- Rollback: restore snapshot 1793939030-bf6baf3d — `VeilSandbox(WS).rollback('1793939030-bf6baf3d')`
- Preserve as artifact: copy mvp-workspace/ to a named artifact

## Result
Project created, tests passed inside the sandbox, no network, no host access.
