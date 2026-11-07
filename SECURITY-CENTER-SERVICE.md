# VEIL Security Center — MVP via separate broker process

Task: task-mvp-service — create a small project, write code, run tests
Snapshot: 1793939903-93e72682 · Broker: separate process, agent socket 0660, admin socket 0600

## What changed vs the in-process MVP
- Agent never imports BrokerV2, never reads policy_v2.example.json, never touches the journal.
- Agent proposes via BrokerClient (agent socket: propose/ping only).
- Approval comes from AdminClient (admin socket: grant/revoke/status/shutdown).
- Sandbox asks the broker over RPC before every bwrap run; broker down = no execution.
- Approvals are in-memory in the broker; a restart clears them.

## Actions
- filesystem.write main.py / test_main.py — allow (via RPC)
- browser.navigate github.com — needs_user_confirmation, then allow with admin token
- terminal.run python3 test_main.py — allow (via RPC), exit 0, ALL TESTS PASSED

## Result
MVP PASSED via separate broker process.
