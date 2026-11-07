# VEIL OS

**An AI-native operating environment, with humans in control.**

> The AI can be powerful, but it can never be more powerful than the permissions the user grants it.

Today's agents are treated like humans using computers — they see screens, click buttons, struggle with interfaces never designed for them.

VEIL reverses that: **make the computer ready for AI from the beginning.**

The AI doesn't control the OS. The OS gives the AI controlled capabilities.

## The idea in one paragraph

VEIL gives every agent a prepared environment before it gets a task: a filesystem it can access, a browser designed for automation, a terminal with controlled commands, API connections you authorize, memory and workspace, a way to install tools, and a task engine.

Everything is gated by a separate security layer — the **VEIL Permission Broker** — which enforces policy *outside* the AI process. If you block internet, the AI cannot bypass it via another tool, a shell command, or a direct network call.

## Strategy: B first, A eventually

- **B — AI sandbox environment (now):** An app that creates isolated computers for AI agents inside existing OSes. Easier to build, test, distribute. This is what we're building first.
- **A — Full AI OS (later):** A Linux-based OS (Arch foundation) where AI is first-class in the desktop. Built on top of B.

Do NOT start by making an Arch ISO. Prove the broker first.

## Layout

```
veil-os/
  README.md                 this file
  ARCHITECTURE.md           six systems, broker flow, threat model, 3 control levels
  ROADMAP.md                Core → Sandbox → Desktop → OS → Ecosystem
  docs/
    ai-native-os-research.md  research supplied by the user (Manus AI, Nov 2026)
    RESEARCH-NOTES.md         what we adopted / rejected from it
  veil-core/
    broker.py               working Permission Broker prototype
    policy.example.json     example user policy (matches your Security Center mock)
    agent_sim.py            untrusted demo agent that tries to bypass the broker
    demo.py                 run the demo: allowed vs blocked, with audit log
  veil-sandbox/
    sandbox.py              REAL Bubblewrap sandbox — broker-gated bwrap runner
    test_sandbox.py         12 bypass tests — all passing
    demo.py                 broker + sandbox combined demo
    etc-minimal/            synthetic /etc for inside the sandbox
  veil-video/
    video_tool.py           broker-gated VeilVideo agent API (probe/transcript/timeline/render)
    cli.py                  `veil-video` CLI (--json for the AI runtime)
    project.py probe.py     project/timeline model, ffprobe, scenes, contact sheets
    transcribe.py render.py faster-whisper wrapper, timeline -> ffmpeg renderer
    jobs.py                 render/transcribe jobs: status, progress, cancel
    remote_api.py           VEIL OS Remote API for the Windows client (:8766)
    remote_client.py        reference Windows client (stdlib-only Python)
    test_video.py           39/39 pass — real ffmpeg, real broker, real files
  environments/
    developer.yaml  researcher.yaml  designer.yaml
    assistant.yaml  analyst.yaml  video-editor.yaml
```

## Quick start — try the broker

```bash
cd veil-os/veil-core
python3 demo.py
```

You should see:
- ✅ file read inside workspace: allowed
- ✅ browser to an allowlisted domain: allowed
- ❌ API connections: blocked (policy)
- ❌ system settings / install software: blocked
- ❌ internet via a "clever" direct request: blocked — the broker, not the agent, decides
- 📋 every request in `audit.log.jsonl`, allowed or denied

## Try the real sandbox

```bash
cd veil-os/veil-sandbox
python3 test_sandbox.py   # 12/12 bypass tests pass
python3 test_landlock.py  # Landlock in-process: 3/3 pass
python3 demo.py           # broker + Bubblewrap combined
```

Real Bubblewrap isolation (bwrap 0.9.0): new user/pid/ipc/net/uts namespaces, `/usr` read-only, `/workspace` is the only host directory mounted, `/home` empty, `/etc` synthetic (no host users/secrets), network loopback-only. `ls /home/hatch` and `cat /etc/shadow` fail inside — not just denied, not mounted. Even approved `python3` gets no socket route.

## Try broker v2 + the MVP

```bash
cd veil-os/veil-core && python3 test_broker_v2.py   # 13/13 pass
cd veil-os/veil-core && python3 test_broker_service.py  # 22/22 pass
cd veil-os && python3 mvp.py                       # Developer MVP, end to end
cd veil-os && python3 mvp_service.py               # MVP via separate broker process
```

Broker v2 uses scoped, expiring capabilities (not boolean switches), four outcomes (allow / deny / needs confirmation / needs step-up), and fails closed. **The broker now runs as a separate process** (`veil-broker.py start/stop/status`): agent socket 0660 (propose/ping only), admin socket 0600 (grant/revoke/status/shutdown). The agent client imports no broker code and cannot read policy or grant itself approval. Kill the broker and the sandbox fails closed — nothing executes. Research v2 is in `docs/veil-os-research-v2.md`, synthesis in `docs/RESEARCH-NOTES-V2.md`.

## Prebuilt environments

| Environment | Already prepared |
|---|---|
| Developer | Editor, terminal, Git, testing |
| Researcher | Browser, search, notes |
| Designer | Design tools, assets, workspace |
| Assistant | Calendar, documents, approved apps |
| Analyst | Python, datasets, notebooks |
| Video Editor | VEIL Video, FFmpeg, faster-whisper, probe, transcript, render |

Pick an environment, grant permissions, give a task. Reset to a clean snapshot when done.

## Business model

OS free and open source. Revenue from hosted environments, managed deployments, enterprise admin, premium backup/sync, optional model/compute. Core stays usable without paying.

## Hardware note

Dev machine: Dell Latitude 7400, 16 GB RAM, integrated graphics. Enough for Core + lightweight sandboxes. No giant model training needed to start.

## Next milestone

VEIL Core: broker + AI runtime + tool interface + audit log — with a test that proves a blocked permission cannot be bypassed from inside the sandbox.

## VEIL Video

An AI-first video-editing subsystem — the AI edits timeline JSON,
not a GUI. See [veil-video/README.md](veil-video/README.md).

```bash
cd veil-os/veil-video
python3 test_video.py   # 39/39 pass (real ffmpeg, real broker)
python3 demo.py         # import → probe → timeline → scenes → render
```

`probe` to see, `transcript` to hear (word timestamps), `timeline`
to edit, `render` as a background job the Windows client can watch.
`video.read/edit/render/transcribe` are automatic inside the
project; `video.publish` needs user confirmation every time and
`video.delete` (source media) needs confirmation + step-up auth.
The Windows Remote Client connects through `remote_api.py`
(`:8766`, Bearer token) to submit tasks, watch jobs, approve
actions, and pause/kill the AI. FFmpeg 8.1.2 is the renderer
(verified live); faster-whisper and melt are **not installed** —
transcribe fails clearly with the install hint instead of faking
a transcript, and `.mlt` export covers Shotcut/Kdenlive.

## Docker image

The whole stack as one runnable image — broker, sandbox, Security
Center, VEIL Video remote API, veil-browser-cli + Chrome, ffmpeg.
See [docker/README.md](docker/README.md).

```bash
cd <workspace>   # contains veil-os/ and veil-browser-cli/ side by side
docker build -f veil-os/docker/Dockerfile -t veil-os:latest .

docker run -d --name veil-os \
  --cap-add=SYS_ADMIN --security-opt seccomp=unconfined \
  -p 127.0.0.1:8765:8765 -p 127.0.0.1:8766:8766 \
  veil-os:latest
# Security Center: http://127.0.0.1:8765/   Remote API: http://127.0.0.1:8766/
```

Or `cd veil-os/docker && docker compose up --build -d`. CI builds the
same image on every push to `main` and publishes it to
`ghcr.io/mymaniscr7-sudo/veil-os:latest`. This is the B-first
deliverable — the bootable Arch ISO (Stage 6) is a separate, later step.

**Verified live 2026-11-06** (image `d2b521b6b72f`, 1.04 GB): the
researcher task browsed github.com through the CLI browser and saved
findings.md via the sandbox, and the video task rendered a 6.0s
640x360 clip with audio — both end-to-end inside the container, with
the broker gating every step. The build surfaced and fixed five real
bugs (Bubblewrap-as-root, Chrome discovery as root, the CLI session
dir, `/opt/veil` permissions for bwrap, the linker's `ld.so.cache`,
the stale-task pid guard, and ffmpeg 5.1's `amix` hang) — see
[docker/README.md](docker/README.md) for the details.

## Related projects

- **[VEIL Browser CLI](../veil-browser-cli/README.md)** — the browser component: a fast command-line browser for AI agents (Rust, CDP, accessibility-tree snapshots with `@eN` refs, screenshots only as a fallback). Integrated as the OS browser via `veil-core/browser_tool.py`: broker-gated (domain checked before Chrome launches, one session per task, no host profile), `browser.navigate`/`browser.act` need confirmation, `browser.read` is auto if the domain is allowed. Verified live: `test_browser_tool.py` 21/21.
