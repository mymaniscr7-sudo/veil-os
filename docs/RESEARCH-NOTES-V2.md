# Research notes v2 — what veil-os-research-v2.md changed

Source: `docs/veil-os-research-v2.md` (supplied by the user, Nov 2026).
Original kept verbatim. This is what we adopted and built.

## Corrected thesis (adopted)

VEIL is **not** "an AI that controls the whole OS." It is **a Linux
desktop that exposes controlled OS capabilities to AI agents through
an independent permission broker.** The model plans and requests; the
broker, OS policy, and isolated executors decide what happens.

## Adopted and built in this round

- **Capability schema v2** (`veil-core/capability.py`, `broker_v2.py`):
  who / what / resources / how long / risk, expire by default (15 min),
  bound to a task/workspace. Four outcomes: allow / deny /
  needs_user_confirmation / needs_step_up_authentication.
  Tests: 13/13 pass (`test_broker_v2.py`).
- **Fail-closed, and a real bug the tests caught:** approval matching
  was `token != stored` — when both were `None` (no approval at all)
  that evaluated False, i.e. self-approval. Fixed to require a
  non-None token that exactly matches the broker-issued token for
  that binding_id. Approval binds to the exact normalized action.
- **Network as a state:** offline / approved-domains / research-web /
  unrestricted-with-confirmation (`policy_v2.example.json`), not a switch.
- **Seven system services** (names now used in docs/code):
  veil-broker, veil-exec, veil-net, veil-secrets, veil-journal,
  veil-snapshot, veil-session. Model runtime talks to veil-broker over
  a narrow RPC; it cannot call systemd, D-Bus, Docker, pacman, host FS,
  or arbitrary sockets directly.
- **Landlock** (`veil-sandbox/landlock.py`, `test_landlock.py`):
  verified in-process on this host — restrict_self succeeds,
  workspace read works, /etc/passwd + ~/MEMORY.md blocked EACCES.
  Exec-after-restrict is unreliable on this host (EACCES even with
  /usr, /lib, /lib64 allowed) — documented, NOT wired as the exec
  path. Bubblewrap is the exec boundary in v1.
- **Tool hierarchy:** 1 semantic adapter, 2 accessibility tree
  (AT-SPI, Vercel agent-desktop), 3 command/RPC, 4 screenshot fallback.
- **MVP** (`mvp.py`, `SECURITY-CENTER.md`): Developer env, "create a
  small project, write code, run tests, show result" — passed end to
  end in the sandbox, with snapshot + rollback verified.

## Adopted in docs (not yet code)

- Browser: separate profile per task, download quarantine,
  broker-controlled egress proxy, no host cookies/passwords/SSH,
  confirm before login/upload/publish/purchase/send/consent.
- Executor levels: Landlock+seccomp low-risk, Flatpak portals for
  desktop apps, container/microVM for untrusted, separate privileged
  helper for host admin (never a general root shell).
- Flatpak portals as the user-consent pattern to borrow — but VEIL
  needs task-oriented, expiring, auditable capabilities, which
  Flatpak's static app permissions don't provide.
- Arch: dev as Arch packages/services + CLI first; ISO via Archiso
  (compare mkosi) only after Core works; pin a known-good package
  snapshot, sign VEIL packages, recovery image, test updates in a
  cloned environment, keep the host minimal.
- Evaluation: OSWorld-style, 11 metrics, negative tests are the most
  important (blocked file, blocked domain, host credential, disabled
  device, denied tool call turned into an equivalent shell command).

## Build order, revised (v2 stages)

1. Core — broker, capability schema, task IDs, JSONL journal,
   cancellation, expiration, CLI, usable without a model.
2. Sandbox — workspace, snapshots, bwrap profiles, Landlock,
   seccomp, cgroups, default-deny network. Tests prove blocked
   file/process/device/network fail.
3. Runtime — one model provider, structured tools, plan state,
   observation, retries, recovery, prompt-injection fixtures.
4. Desktop — task view + Security Center, capabilities, approvals,
   actions, rollback, environment selection/reset.
5. Native adapters — filesystem, process, browser, editor, terminal,
   accessibility-tree. Screenshots only when no semantic interface.
6. OS image — Archiso profile, installer, signed repo, recovery,
   update channel, hardware list.

## What not to do (v2 checklist)

No unrestricted root shell. No host Docker socket. No env vars into
the workspace. No credentials in prompts/screenshots. No prompt
instructions as the injection defence. No model as final approval
authority. No "container alone = safe". No new kernel/filesystem/
model training until the prototype proves a limitation requires it.
No model-composed bwrap arguments — veil-exec generates a fixed
profile from a validated manifest.
