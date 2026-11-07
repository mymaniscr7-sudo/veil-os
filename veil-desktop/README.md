# VEIL Security Center — Desktop UI (Stage 3, v1)

A view + control client for policy state. It is **not** the policy
itself: every toggle, approval and rollback goes through the broker
service (admin socket) or the sandbox. The UI never edits policy
files directly and never grants itself approval.

## Run

```bash
python3 server.py [--port 8765]
# open http://127.0.0.1:8765/  (binds 127.0.0.1 only)
# the server starts the broker if it isn't running
```

`index.html` also runs as a preview with `?demo=1` (simulated
broker state, buttons work locally) — that's what the artifact
preview uses. The real UI has no `?demo`.

## What it shows (research v2)

- **Current task** — name, objective, status, current step, pid
- **Pending approvals** — exact action, target, expected effect,
  risk, session, scope. Not just "Allow AI to continue?"
- **Network** — a state, not a switch: offline / approved-domains /
  research-web / unrestricted-with-confirmation. Changing it
  updates broker policy immediately, saves the policy file, and
  going offline revokes network approvals on the spot.
- **Capabilities** — allowed/blocked per capability, with
  Allow/Block toggles that actually change policy.
- **Active approvals** — bound to the exact action, with expiry
  countdown (15 min default).
- **Recent actions** — journal, newest first, with outcomes.
- **Snapshots** — rollback to a snapshot (restores the workspace
  it came from; `.veil-meta.json` never leaks into a rollback).
- **Resource limits** — CPU/AS/processes/file/timeout, plus the
  honest cgroup probe (not enforced on this host).

## Controls that do not depend on the model

- **Pause AI** — SIGSTOP to the task process (proc state `T`).
- **Resume** — SIGCONT (proc state `S` again).
- **Kill task** — SIGTERM, caught by the task, recorded as
  "killed by user in Security Center". Killing also clears the
  task's pending approvals, so a dead task can't be approved
  after the fact.

## Demo task

`demo_task.py` runs a real Developer task through the broker
service + sandbox and waits for the user when it needs approval
(browser.navigate to github.com). Start it from the UI
("Start demo task") or via `POST /api/task/start`.

## API (all under `/api/`)

- `GET /api/state` — broker, policy, network, capabilities,
  pending, approvals, journal, snapshots, task, limits
- `POST /api/approve {proposal_id}`, `POST /api/deny {proposal_id}`
- `POST /api/network {state}`, `POST /api/capability {capability, allowed}`
- `POST /api/snapshot/rollback {snapshot_id}`
- `POST /api/task/start|pause|resume|kill`

## Broker changes that make this UI work

- `broker_v2.py`: pending queue (`list_pending`, `approve_pending`,
  `deny_pending`), approval expiry (tokens expire with the
  capability), denied-bindings memory (a denied action re-proposed
  within 5 min returns `deny` promptly, not `needs_*` again),
  stored-approval retry (if the user approved the exact action,
  the agent retrying without a token is allowed — the broker holds
  the approval; a wrong token still fails), `set_network_state` /
  `set_capability` with atomic policy save.
- `broker_service.py` / `broker_client.py`: admin methods
  `list_pending`, `list_approvals`, `approve_pending`,
  `deny_pending`, `set_network_state`, `set_capability`,
  `get_policy`, `reload_policy`.
- Tests: broker v2 18/18, broker service 22/22.

## Verified live (2026-11-05)

- Approve flow: task waited, approval in UI → continued, tests
  passed, install denied by policy, status `done`.
- Deny flow: task stopped promptly with `denied`.
- Network off: browser propose denied immediately, policy file
  persisted `offline`, restored to `approved-domains`.
- Pause → proc `T`, Resume → proc `S`, Kill → `killed` recorded.
- Rollback via API: workspace restored, no `.veil-meta.json` leak.

## Browser sessions

`/api/state` includes `browser_sessions` — live veil-browser-cli
sessions (one per VEIL task) with tabs + active URL, and
`browser_cli` (binary availability). Only live daemons are listed:
querying a stopped session would auto-start its daemon, so the
server checks for the daemon socket first.

Start the researcher task from the UI ("Start researcher task"):
it opens github.com through the broker-gated browser (needs your
approval), snapshots + reads via the accessibility tree, and saves
`research-workspace/findings.md`.
