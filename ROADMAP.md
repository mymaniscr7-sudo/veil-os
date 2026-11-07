# VEIL Roadmap — B first, A eventually

## Stage 1 — VEIL Core (now)
Build the permission broker, AI runtime, tool interface, and audit log.
- [x] Broker prototype (`veil-core/broker.py`) — policy check + audit log
- [x] Demo untrusted agent that tries to bypass it
- [x] Broker as a separate process / socket service (not in-process) — done 2026-11-05, `broker_service.py`, tests 22/22
- [ ] Tool interface: `file.read/write`, `browser.open`, `terminal.run` — all via broker
- [x] Approval flow: broker pauses, asks user, resumes or denies — verified live via Security Center demo task
- [x] Resource limits: CPU / memory / time / network bytes per task — `limits.py` + seccomp, tests 11/11 (cgroups NOT enforced on dev host; see Limits section)
- [x] Capability schema v1: scoped capabilities + expiry + risk + 4 action classes (research) — broker v2, tests 18/18
- [ ] Tool contract: propose → validate → execute → observe → rollback, with `expected_effect` check

**First milestone:** a blocked permission cannot be bypassed from inside the sandbox. Prove it with a test. — *Proved for the broker in-process demo, and for the real sandbox: `veil-sandbox/test_sandbox.py` 12/12 pass.*

## Stage 2 — VEIL Sandbox
Isolated workspaces, browser access, terminal restrictions, filesystem controls.
- [x] Bubblewrap sandbox for light tasks (`veil-sandbox/sandbox.py`, bwrap 0.9.0) — workspace-only FS, no network, synthetic /etc, snapshot/rollback, 12/12 bypass tests
- [ ] Firecracker microVM for high-risk tasks
- [x] Virtual workspace filesystem, snapshot + reset
- [ ] Allowlisted network via broker proxy only
- [ ] gVisor layer for hardened ordinary tasks (research progression)
- [ ] Red-team prompt-injection tests (web pages, docs, filenames, terminal output, tool descriptions)

## Stage 3 — VEIL Desktop
Actual interface: Security Center dashboard, prepared environments, task control.
- [x] Security Center UI (the switches in the original mock, wired to real policy) — done 2026-11-05, `veil-desktop/server.py`, live at http://127.0.0.1:8765/
- [ ] Environment picker: Developer / Researcher / Designer / Assistant / Analyst
- [ ] Task view: what the agent is doing, what it asked for, what was denied

## Stage 4 — VEIL OS
Package into a bootable Linux distribution (Arch base) with automatic setup and recovery.
- [x] Docker image — the B-first deliverable (2026-11-06): multi-stage
  Dockerfile (`docker/Dockerfile`) builds the whole stack (broker,
  sandbox, Security Center, VEIL Video remote API, veil-browser-cli +
  Chrome, ffmpeg) into one runnable image; compose file + CI publish to
  GHCR on every push to main. Run: `docker compose up --build -d`
  (needs `--cap-add=SYS_ADMIN --security-opt seccomp=unconfined` for
  Bubblewrap). The bootable ISO below is still the later step.
- [ ] Arch-based image, VEIL Desktop as default session
- [ ] First-boot setup: user, model provider, environments
- [ ] Recovery: reset to clean snapshot

## Stage 5 — VEIL Ecosystem
Environment packages, plugins, model providers, community contributions.
- [ ] Environment registry
- [ ] Plugin API (broker-gated, like tools)
- [ ] Hosted environments / managed deployments (business)

## Non-goals for now
- Writing a kernel
- Making an Arch ISO before Core works
- Training a model

## v2 build order (research v2, supersedes the stage list above where they differ)
1. Core — broker, capability schema, task IDs, JSONL journal, cancellation, expiration, CLI, usable without a model. [x] broker v2 + capability schema + 13/13 tests. MVP Developer task passed end-to-end.
2. Sandbox — workspace, snapshots, bwrap profiles, Landlock, seccomp, cgroups, default-deny network. [x] bwrap 12/12, Landlock in-process 3/3. [ ] seccomp/cgroups profiles, red-team prompt-injection tests.
3. Runtime — one model provider, structured tools, plan state, observation, retries, recovery, prompt-injection fixtures. Model output untrusted, every action validated.
4. Desktop — task view + Security Center (capabilities, approvals, actions, rollback), environment selection/reset. [~] SECURITY-CENTER.md report is the text version of this.
5. Native adapters — filesystem, process, browser, editor, terminal, accessibility-tree. Screenshots only when no semantic interface.
6. OS image — Archiso profile, installer, signed repo, recovery, update channel, hardware list. Only now does "VEIL OS" become primarily a bootable distro.

## Broker separation — done (2026-11-05)
- [x] Broker as a separate process / socket service (was Stage 1 open item)
  - broker_service.py: agent socket 0660 (propose/ping), admin socket 0600 (grant/revoke/status/shutdown)
  - broker_client.py: thin RPC, no policy/broker imports on the agent side
  - veil-broker.py CLI: start/stop/status/grant/propose
  - Sandbox defaults to service mode, fails closed with no in-process fallback
  - Tests: test_broker_service.py 22/22 (kill broker, self-approval, socket separation, restart clears approvals)
  - MVP via service: mvp_service.py passed end to end
  - Fixed: terminal.run fell through to external-side-effect; now reversible-write with approved-commands check
  - Remaining hardening (production): run broker as dedicated user `veil`, agent as `veil-agent`, so socket permissions enforce UID separation, not just method allowlists. On this dev host everything is the same UID, so that layer is documented, not pretend-enforced.

## Limits + seccomp — done (2026-11-05)
- [x] ResourceLimits via rlimits + timeout (limits.py), seccomp filter via libseccomp ctypes (seccomp.py)
- [x] Wired into VeilSandbox.run() by default; tests 11/11 (test_limits_seccomp.py)
- [x] Cgroup probe reports honestly: v2 controllers empty, MemoryMax ignored — NOT claimed as enforced
- [ ] Production: cgroup v2 with controllers enabled (memory.max/cpu.max/pids.max), or systemd with working MemoryMax; dedicated UID per sandbox for real per-sandbox NPROC limits
- [ ] Future tightening: block unshare/setns/clone3 after testing runtime breakage

## Security Center UI — done (2026-11-05)
- [x] veil-desktop/server.py + index.html (127.0.0.1:8765, stdlib only)
- [x] Broker admin methods: pending queue, approval expiry, deny memory, stored-approval retry, network/capability toggles that actually change policy
- [x] Demo task that waits for UI approval; approve/deny/network-off/pause/resume/kill/rollback all verified live
- [x] Artifact preview (static demo mode ?demo=1)
- [ ] Real desktop app shell (Electron/Tauri/GTK) — the web UI is the view, a native shell would add tray + notifications
- [ ] Step-up auth for critical actions (currently needs_step_up is a broker outcome; UI has no step-up flow yet)
- [ ] Multi-task support (v1 assumes one demo task at a time)

## Browser CLI integration — done (2026-11-06)
- [x] veil-core/browser_tool.py (VeilBrowser, broker-gated) + test_browser_tool.py 21/21
- [x] capability.py: browser.act = external-side-effect, browser.screenshot = read
- [x] Researcher env wired to veil-browser-cli (environments/researcher.yaml)
- [x] Security Center shows live browser sessions; researcher demo task (research_task.py) verified live: approve -> github.com snapshot/read -> findings.md
- [x] Fixed: _current_host() and Security Center session listing never auto-start a daemon (socket check first)
- [ ] Run Chrome inside Bubblewrap / microVM (hardening, later)
- [ ] veil-net egress proxy for broker-only network (sandbox network is OFF in v1 until then)
- [ ] Step-up auth flow in Security Center for critical browser actions (login, purchase)
