# Research notes — what the ai-native-os research changed in VEIL

Source: `docs/ai-native-os-research.md` (Manus AI, Nov 2026, supplied by the user).
Original is kept verbatim in this folder. This note is what we adopted, what we rejected, and what it changes in the build order.

## Adopted — and now in VEIL

1. **Capability OS, not just a permission switch.** Every action is a typed capability with scope, risk, expiry. VEIL policy is moving from boolean switches (`browser: allowed`) to scoped capabilities (`filesystem.write`, scope `/workspace/project-a/**`, expires in 900s, risk medium). The broker prototype still uses the boolean policy for v1; the capability schema is the next broker upgrade.
2. **Three levels of control, in order.** A: semantic tools first (`read_file`, `write_file`, `move_file`, `install_package`…) — fast, reliable. B: app adapters (browser tabs, editor via language server, terminal allowlist). C: GUI fallback (screenshot / mouse / keyboard on a virtual display) — compatibility only, never the foundation. VEIL previously implied "browser designed for automation" — this makes the ordering explicit.
3. **Control plane, five services.** Intent Manager, World Model, Policy Engine, Tool Router, Journal/Replay Log. The broker is the Policy Engine + Journal; the AI Runtime gains the other three. Tool Router rule: native API first, command runner second, GUI third.
4. **Tool contract: propose → validate → execute → observe → rollback.** The model must state `expected_effect`. The executor checks the actual change against it and can restore a snapshot. Sandbox v1 already implements this shape: `run()` snapshots first, returns a rollback token (snapshot id).
5. **Four action classes.** Read (auto in workspace), Reversible write (auto in workspace, with snapshot), External side effect (confirm — send/publish/change account), Irreversible/privileged (explicit confirm + step-up auth — delete, change permissions, install host software, money, deploy). "Allow AI to continue?" is not sufficient — the UI must show the exact operation, target, and parameters.
6. **Never trust the model about approval.** A model-generated `user_confirmed: true` must never be trusted. The policy service verifies actor, tool, normalized parameters, target, expiry, one-time use.
7. **Isolation progression.** Rootless containers / Bubblewrap for prototype and light tasks → gVisor for hardened ordinary tasks → Firecracker microVM per session/task for untrusted or multi-tenant. Seccomp is a layer, not a sandbox. MCP is an integration protocol, not a security boundary — authorization lives below it in the broker.
8. **Stack.** Rust or Go for the privileged broker/executor, Python/TypeScript for the model loop and adapters, JSON Schema for tool contracts, SQLite for journal/policy, Xvfb/Wayland for the virtual desktop.
9. **Evals before expanding authority.** OSWorld-style executable tasks. Track success, time, tool-call count, recovery rate, unauthorized-action rate, exfiltration rate, rollback success, cost per completed task. Don't expand permissions because the agent sounds confident.

## Rejected / deferred

- **Starting on Ubuntu/Debian for the prototype host.** Research suggests Ubuntu/Debian for Phase 0. VEIL's eventual OS is Arch-based, but the dev host here is Ubuntu and the sandbox is host-agnostic — we keep Ubuntu for dev, Arch for the eventual image. No conflict.
- **Full virtual desktop in Phase 0.** Useful, but VEIL's differentiator is semantic tools + prepared environments, not clicking. GUI fallback stays Level C, after Core + Sandbox work.

## Build order, revised (research Phases mapped to VEIL)

- Phase 0 (2–3 days): prove the control loop — supervisor + `open_app`/`read_file`/`write_file`/`run_safe_command` + approval panel + JSONL journal. *VEIL status: broker demo + sandbox tests do the file/command half of this.*
- Phase 1 (1–2 weeks): world model, structured schemas, snapshots, cancellation, timeouts, quotas, policy engine, browser/file/process/terminal adapters, local web UI with plan/state/approval/journal.
- Phase 2 (2–4 weeks): harden execution — containers/microVMs, egress allowlists, secret injection at execution time only, read-only base, package-cache controls, limits, teardown, red-team prompt-injection tests (web pages, docs, filenames, terminal output, tool descriptions).
- Phase 3: AI shell as primary launcher, "Prepare my release workspace", persistent memory only after validation/isolation, "why did you do that?" view from the journal.
- Phase 4: evaluate before expanding authority (OSWorld-style).

## What not to do (checklist, from research)

No unrestricted root shell. No host Docker socket mount. No env vars passed into the workspace. No credentials in prompts or screenshots. No prompt instructions as the injection defence. No model as final approval authority. No "container alone = safe" assumption. No new kernel/filesystem/model training until the prototype proves a limitation requires it.
