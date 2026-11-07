# VEIL Architecture

## Six systems — and the seven services inside them

```
VEIL Desktop · AI Runtime · VEIL Permission Broker
  ↓
Browser / Filesystem / Terminal / Network / APIs / Memory
  ↓
Host OS — Linux
```

**System services (v2 names):** `veil-broker` (policy + auth),
`veil-exec` (only service allowed to launch executors — it generates
fixed bwrap profiles from validated manifests, the model never composes
bwrap arguments), `veil-net` (network policy + egress proxy),
`veil-secrets` (short-lived credential release + redaction),
`veil-journal` (append-only log), `veil-snapshot` (checkpoints +
rollback), `veil-session` (task lifecycle, quotas, cancellation,
cleanup). The model runtime talks to `veil-broker` over a narrow
local RPC. It cannot call systemd, D-Bus, Docker, pacman, the host
filesystem, or arbitrary sockets directly.

**Corrected thesis:** VEIL is not "an AI that controls the whole OS."
It is a Linux desktop exposing controlled OS capabilities to AI agents
through an independent broker. See `docs/RESEARCH-NOTES-V2.md`.

## The broker flow

1. AI requests an action: `browser.open("https://example.com")`
2. Broker checks user policy (not the AI's claim about policy)
3. Broker checks domain allowlist, resource limits, approval state
4. If allowed: broker executes inside the isolated environment, records it
5. AI receives only the result — never a raw capability

If the user blocks internet, the block lives in the broker + network namespace, not in a prompt. A shell command, a second tool, or a direct socket cannot route around it because the sandbox process has no route.

## Policy model (v0 → v1)

A policy is a user-owned JSON document. Example in `veil-core/policy.example.json`:

- `browser`: allowed, domains allowlisted
- `internet`: allowed (but only via broker)
- `api_connections`: **blocked**
- `file_access`: allowed, scoped to workspace
- `terminal`: allowed, approved commands only
- `system_settings`: **blocked**
- `install_software`: **blocked**
- `camera_microphone`: **blocked**

Permissions are capabilities, not suggestions. Default-deny: anything not explicitly allowed is denied.

**v1 direction (from research, see `docs/RESEARCH-NOTES.md`):** move from boolean switches to scoped capabilities — `{capability: "filesystem.write", scope: "/workspace/project-a/**", operations: ["create","modify"], expires_in_seconds: 900, risk: "medium"}` — plus four action classes: Read (auto in workspace), Reversible write (auto in workspace, with snapshot), External side effect (confirm), Irreversible/privileged (explicit confirm + step-up auth). Never trust a model-generated `user_confirmed: true`; the policy service verifies actor, tool, normalized parameters, target, expiry, and one-time use.

## Three levels of control (research)

Use the cheapest reliable interface, in order:

- **Level A — semantic tools (first):** `read_file`, `write_file`, `move_file`, `start_process`, `open_url`, `install_package` — typed JSON schemas, narrow scope. Fast, no image reasoning.
- **Level B — app adapters:** browser tabs/downloads, editor via language server, terminal allowlist profile. Real state, not pixels.
- **Level C — GUI fallback (last):** virtual display + screenshot/mouse/keyboard, for apps with no adapter. Compatibility layer, never the foundation.

Tool Router rule: native API first, command runner second, GUI third.

## Tool contract

```
propose(action, parameters, expected_effect) -> proposal_id
validate(proposal_id) -> allow | deny | needs_confirmation
execute(proposal_id) -> result, events, rollback_token
observe() -> structured_state + optional_screenshot
rollback(rollback_token) -> result
```

The model must state what it thinks will change. The executor checks the actual change against that expectation and restores a snapshot when possible. The sandbox already implements the snapshot/rollback half of this; `expected_effect` checking is next.

## Control plane (research)

Intent Manager (plan + state) · World Model (structured view of files/windows/apps/processes/network/tasks) · Policy Engine (the broker) · Tool Router · Journal/Replay Log (append-only, JSONL/SQLite). The model never directly owns a privileged socket, host filesystem, cloud credential, or unrestricted terminal.

## Threat model — what the broker must survive

- Agent tries a different tool to do a blocked thing → denied, same policy
- Agent runs a shell command to `curl` a blocked domain → no network namespace route, denied
- Agent reads a credential file directly → filesystem is virtualised, credentials are broker-managed, never mounted in the sandbox
- Agent tries to install a package to gain a tool → install is a broker action, denied
- Agent tries to edit policy → policy file is outside the sandbox, owned by the user

## Layers (Linux, no new kernel)

1. **Linux foundation** — Arch Linux (for the eventual OS image) or Ubuntu for dev. Hardware, drivers, processes, memory, networking.
2. **VEIL Desktop** — launcher, AI interface, task manager, Security Center.
3. **VEIL Sandbox** — namespaces, cgroups, seccomp, Bubblewrap for light tasks, Firecracker microVMs for high-risk tasks. **Status: Bubblewrap v1 built and tested** (`veil-sandbox/` — 12/12 bypass tests pass). Landlock verified in-process (see `veil-sandbox/landlock.py`); exec-after-restrict is unreliable on this host and is NOT the exec path in v1. Progression: Bubblewrap → gVisor (hardened ordinary tasks) → Firecracker microVM per task (untrusted / multi-tenant). Seccomp is a layer, not a sandbox. MCP is an integration protocol, not the security boundary — authorization lives below it in the broker. Bubblewrap warning (v2): bwrap builds a sandbox, but the caller's arguments define the policy — mounting a D-Bus socket, host directory, device, or powerful IPC endpoint can undermine it. The model must never compose bwrap arguments.
4. **VEIL AI** — model-independent runtime: local models, cloud APIs, tools, workflows. Broker/executor in Rust/Go eventually; model loop in Python/TypeScript now.

## Executor levels (v2)

- Low-risk local adapters: Landlock + seccomp + resource limits + scoped env.
- Desktop applications: Flatpak-style sandboxing and portals where compatible.
- Untrusted code / web / plugins / autonomous tasks: container or microVM.
- Host administration: separate privileged helper, explicit narrow operations, never a general root shell.

## Browser and network (v2)

Separate browser profile per task, separate filesystem + download quarantine, broker-controlled egress proxy, domain allowlists + redirect handling, no host cookies / password stores / SSH agents / personal profiles, confirmation before login / upload / publish / purchase / send / consent, record of page + domain + action + policy decision. Network states: `offline`, `approved-domains`, `research-web`, `unrestricted-with-confirmation`. Landlock cannot do domain policy (ports, not domains) — that lives in the egress proxy / firewall layer.

## Arch product strategy (v2)

Dev first as Arch packages/services + CLI on a normal Arch install/VM (`veil-core`, `veil-broker`, `veil-exec`, `veil-net`, `veil-secrets`, `veil-journal`, `veil-snapshot`, `veil-desktop`, `veil-env-*`, `veil-cli`). Distribution via Archiso (compare `mkosi`) only after Core works. First ISO is a pinned, tested release — not "latest Arch + AI packages": pinned package snapshot, signed VEIL repo, kernel/compositor/portals/browser/broker/executor tested together, recovery image + rollback, updates tested in a cloned environment first, host kept minimal.

## Environments

An environment is a declarative bundle: tools + workspace template + default policy + snapshot.

```yaml
name: developer
tools: [editor, terminal, git, pytest]
workspace: /workspace
default_policy: developer-policy.json
snapshot: clean
```

Reset = restore snapshot. No leftover state, no leaked credentials.

## What "done" looks like for Core

- Broker runs as a separate process / user from the agent
- All agent capabilities go through the broker — no direct host syscalls for gated resources
- Audit log proves every allow/deny, with reason
- A test agent that actively tries to bypass the broker fails every bypass, in CI

## Security Center (Stage 3, v1)

`veil-desktop/server.py` serves `index.html` on 127.0.0.1:8765.
It is a view + control client only. Policy lives in the broker
process; the UI talks to it over the admin socket. Toggles call
`set_network_state` / `set_capability`, which mutate broker
policy in-memory AND save the policy file atomically. Approvals
are broker-issued tokens bound to the exact normalized action
(binding_id) and expire with the capability. Pause/Kill are
OS signals to the task process, not requests to the model.

## Browser — veil-browser-cli is the OS browser (Stage 3)

The AI does not get a visual browser. It gets `veil-browser-cli`
(Rust, CDP, Chrome for Testing) through `veil-core/browser_tool.py`
(`VeilBrowser`). `snapshot` to see, `@eN` to act — compact
accessibility text, no screenshots by default, no HTML soup.

Gating (broker first, Chrome second):
  - `browser.navigate` / `browser.act` = external side effect,
    needs user confirmation. `browser.read` / `snapshot` / `scroll`
    = read class, auto if the domain is allowed.
  - Domain checked against policy BEFORE Chrome ever launches.
    A denied domain creates no CLI session at all (verified in
    tests: no session dir). `_current_host()` never starts a
    daemon to ask what page is open — it checks for the daemon
    socket first, so a denied read/click also never launches Chrome.
  - One CLI session per VEIL task (`veil-<task>`): own daemon
    socket, Chrome profile, cookies, tabs, debug port. No host
    profile, no host cookies/passwords. Screenshots go to the task
    workspace only. `file://` URLs rejected locally.
  - Network state enforced by the broker: offline denies all
    browser.* capabilities before any CLI call.
  - No downloads / file uploads / login automation: the CLI has no
    such primitives, by design. Those are broker decisions.

Limits (honest): Chrome runs as its own process with its own
sandbox, NOT inside Bubblewrap v1. Running it inside bwrap or a
microVM is a later hardening step. The Security Center lists live
browser sessions (only live daemons — querying a stopped session
would resurrect it, so the server checks for the daemon socket
first) with tabs + active URL.

## VEIL Video (2026-11-06)

AI-first video editing: the AI edits timeline JSON, never a GUI.
`veil-video/` — project model (`project.py`), ffprobe/scene/contact
sheet (`probe.py`), faster-whisper transcripts with word timestamps
(`transcribe.py`), deterministic timeline -> ffmpeg rendering
(`render.py`), background jobs with progress/cancel (`jobs.py`),
broker-gated agent API (`video_tool.py`, mirrors `browser_tool.py`),
CLI (`cli.py`), Remote API for the Windows client (`remote_api.py`,
:8766, Bearer token), reference Windows client (`remote_client.py`).

Capabilities: `video.read` (read), `video.edit` / `video.render` /
`video.transcribe` (reversible-write inside the project),
`video.export` / `video.publish` (external side effect — user
confirmation every time), `video.delete` (irreversible/privileged
at critical risk — confirmation + step-up). Media is copied into
`media/` on import; renders stay in `renders/`; publish is the
only way a file leaves the project, and only with approval.
Broker down -> fail closed. Long ops are jobs the Windows client
polls; there is no WebSocket in v1 (polling over HTTPS, 2-5s).

Honest gaps (checked on the dev host): faster-whisper NOT
installed — transcribe fails with the install hint, never fakes a
transcript. melt/MLT NOT installed — ffmpeg is the v1 renderer;
`.mlt` export is Shotcut/Kdenlive interop only. Remote API is
plain HTTP on loopback in v1 — SSH tunnel / WireGuard / TLS proxy
in front before exposing it to Windows over a network.
