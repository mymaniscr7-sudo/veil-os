# VEIL OS — Docker image

One image, whole stack: Permission Broker, Bubblewrap sandbox, Security
Center, VEIL Video remote API, veil-browser-cli + Chrome for Testing,
ffmpeg. The AI runs inside the container; you supervise it from the
Security Center or the Windows remote client.

## Build

The build context is the **workspace root**, because the image vendors
the browser CLI from source and the two repos live side by side:

```
<workspace>/
  veil-os/            # this repo
  veil-browser-cli/   # sibling repo, built from source in stage 1
```

```bash
cd <workspace>
docker build -f veil-os/docker/Dockerfile -t veil-os:latest .
```

Or with compose (same build, plus the runtime flags and volumes):

```bash
cd veil-os/docker
docker compose up --build -d
```

Published images are also built by CI on every push to `main` — see
`.github/workflows/docker.yml` — at
`ghcr.io/mymaniscr7-sudo/veil-os:latest` (once the GitHub repos exist).

### Building behind a proxy

If your build host reaches the internet through an HTTP proxy, the
build needs it in two places: the Docker daemon (for pulling the base
images — configure it in the daemon's environment) and the `RUN` steps
inside the build (for `apt`, `cargo`, and the Chrome download). Pass
them as build args:

```bash
docker build -f veil-os/docker/Dockerfile -t veil-os:latest \
  --build-arg HTTP_PROXY="$HTTP_PROXY" \
  --build-arg HTTPS_PROXY="$HTTPS_PROXY" \
  --build-arg NO_PROXY="$NO_PROXY" .
```

Note: build args can show up in image layer history, so don't pass a
proxy URL that embeds a password you can't rotate.

If the proxy also MITMs TLS (this dev host's Hatch egress proxy does),
`cargo` and `curl` inside the build will reject the proxy's certificate
until the proxy's CA is trusted. Pass the CA as base64 — it's a public
trust anchor, not a secret, and the step is a no-op when the arg is
empty (as in CI):

```bash
docker build -f veil-os/docker/Dockerfile -t veil-os:latest \
  --build-arg HTTP_PROXY="$HTTP_PROXY" \
  --build-arg HTTPS_PROXY="$HTTPS_PROXY" \
  --build-arg HATCH_CA_B64="$(base64 -w0 /etc/ssl/certs/hatch-egress-ca.pem)" .
```

### Running on a proxied host

The image itself carries no proxy settings (most hosts don't need
one). If your host reaches the internet through a proxy, the browser
CLI inside the container needs it at **run** time — its local shim
forwards Chrome's requests to the egress proxy from `HTTPS_PROXY`:

```bash
docker run -d --name veil-os ... \
  -e HTTP_PROXY="$HTTP_PROXY" -e HTTPS_PROXY="$HTTPS_PROXY" \
  -e NO_PROXY="$NO_PROXY" veil-os:latest
```

### Podman notes (rootless / restricted hosts)

The image also builds and runs under Podman (`podman build` /
`podman run` with the same flags), which is how it was first verified
on the dev host — Docker's daemon can't start containers there because
the host itself sits inside a user namespace. Two caveats, both host
limitations, not image bugs:

- Podman builds OCI-format images by default, which drop the
  `HEALTHCHECK` — pass `--format docker` to `podman build` to keep it.
- On a host without working cgroups, add `--cgroups=disabled` to
  `podman run`. And `podman exec` doesn't work inside a nested user
  namespace (it needs a `setns` from outside that the kernel denies);
  use the container's own HTTP APIs instead — the Security Center
  (`:8765`) and the Remote API (`:8766`) are the way in.
- On this dev host, `podman build` intermittently fails at a random
  layer step with `operation not permitted` writing a layer temp
  file (`.tmp-volatile-layers.json` / `.tmp-mountpoints.json`) or
  the events log — even on a clean storage, and even though the
  same paths are writable by hand. It is flaky, not deterministic:
  a plain retry (same command) usually succeeds within 1–3 tries,
  and a build run under `strace -f` succeeded first time. If you
  hit it, just retry the build; don't chase the filesystem.

## Run

```bash
docker run -d --name veil-os \
  --cap-add=SYS_ADMIN --security-opt seccomp=unconfined \
  -p 127.0.0.1:8765:8765 -p 127.0.0.1:8766:8766 \
  -v veil-workspace:/workspace \
  -v veil-run:/opt/veil/run \
  -v veil-video-projects:/opt/veil/veil-video/projects \
  veil-os:latest
```

- **Security Center:** http://127.0.0.1:8765/ — task, pending approvals,
  capabilities, network state, journal, snapshots, video projects.
  It has **no auth by design** (it is a local UI), so the port is
  published on `127.0.0.1` only. Do not publish it on `0.0.0.0`.
- **Remote API (Windows client):** http://127.0.0.1:8766/ — Bearer token
  required. On first boot the entrypoint generates a token, saves it to
  `run/remote-token` (0600, in the `veil-run` volume) and prints it once
  in the container log: `docker logs veil-os`. To choose your own token,
  set `VEIL_REMOTE_TOKEN` before the first boot
  (`VEIL_REMOTE_TOKEN=... docker compose up -d`, or a `.env` file).

Why `--cap-add=SYS_ADMIN --security-opt seccomp=unconfined`? Bubblewrap
creates user namespaces and mounts filesystems, which Docker's default
seccomp profile blocks and which need `CAP_SYS_ADMIN`. Without them the
sandbox (and therefore every agent task that runs code) fails. `--privileged`
works too if your host needs it; the two flags above are the smaller set.

## What's inside

| Layer | Contents |
| --- | --- |
| Stage 1 — `rust:1-bookworm` | Builds `veil-browser` from `veil-browser-cli/` (locked Cargo deps) |
| Stage 2 — `python:3.12-slim-bookworm` | Python 3.12, ffmpeg, Bubblewrap, libseccomp, Chrome for Testing (stable), `veil-browser`, all of `veil-os/` |
| Runtime user | `root` inside the container (see "Why root" below). Chrome was installed as `veil` (uid 1000); the runtime runs as root. |
| Services | Broker (Unix sockets in `/opt/veil/run`), Security Center `:8765`, video Remote API `:8766` |
| Volumes | `/workspace` (task workspaces), `/opt/veil/run` (broker state + remote token), `/opt/veil/veil-video/projects` (video projects + renders) |

### Why root

Bubblewrap 0.8.0 only works setuid-root or as uid 0, and neither
works for a non-root user in this image: the rootfs is mounted
`nosuid` (so a setuid bwrap is ignored), and a non-root user can't
write bwrap's two-line uid_map (it needs `CAP_SETUID`). As uid 0
bwrap inherits the container's caps and the sandbox works. The
container itself is the isolation boundary from the host; the broker
still gates every action. The `veil` user still exists (Chrome was
installed under it) and the production split (separate `veil` /
`veil-agent` users) is a later step.

Two more root-related details the image already handles, so you
don't have to rediscover them:

- `VEIL_BROWSER_CHROME` is set explicitly to
  `/home/veil/.cache/veil-browser/chrome-linux64/chrome`. The CLI
  finds Chrome via `$HOME` by default, and root's `$HOME` is `/root`,
  not `/home/veil` — without the explicit path the daemon fails
  with "no Chrome/Chromium binary found".
- `browser_tool.py` resolves the CLI session dir from
  `VEIL_BROWSER_HOME` first (same rule as the Rust CLI), not from
  `Path.home()`. Root's home would otherwise make every
  `_current_host()` check report "no page open" after a successful
  navigate.
- `/opt/veil` is made world-readable (`chmod -R a+rX`) at build
  time: bwrap's inner root runs in a nested user namespace, where
  its `CAP_DAC_OVERRIDE` does not reach files owned by the outer
  namespace, so the sandbox's `--ro-bind` of `etc-minimal/*` (0600,
  veil-owned) failed with "Permission denied". Secrets live in
  `run/` (a volume), not in `/opt/veil`.
- The sandbox binds `/etc/ld.so.cache` into the container's
  `/etc` (a fresh tmpfs): the inner python is
  `/usr/local/bin/python3` with libs in `/usr/local/lib`, which
  the linker only finds via the cache. Without it the inner
  command fails with "libpython3.12.so.1.0: cannot open shared
  object file".

## Verify it works

Verified live 2026-11-06 (image `d2b521b6b72f`, 1.04 GB, Podman 6.1.4
on the dev host — Docker's daemon can't start containers there):

- `GET /api/state`: broker ok, `bwrap_available: true`, browser CLI
  present, video projects listed.
- **Researcher task: done.** Approved `browser.navigate` to
  github.com; the CLI opened it (title "GitHub · Change is
  constant…"), snapshot + read via the accessibility tree, and
  findings.md was saved through the sandbox. This is the step that
  failed before the fixes below.
- **Video task: done.** "render done — vid-df4c62aa: 6.0s, 640x360,
  audio: True (ffmpeg, broker-gated)"; `render.mp4` (167 KB) on disk
  in the `veil-video-projects` volume. The Remote API cross-check
  (`GET /api/video/state` with the Bearer token) lists the project
  and the render job as done.
- The Security Center task guard was fixed along the way: a finished
  task left its pid behind (zombie, never reaped), so `os.kill(pid, 0)`
  still succeeded and no second task could ever start. `_start_task`
  now treats the pid as stale when `task.json` already reports a
  terminal state (done/failed/denied/killed).

```bash
# Security Center answers (and the broker is up behind it)
curl -s http://127.0.0.1:8765/api/state | python3 -m json.tool | head -30

# Remote API answers with the token from the first-boot log
TOKEN=$(docker exec veil-os cat /opt/veil/run/remote-token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8766/api/video/state

# Browser CLI works inside the container (opens a real page, no GUI)
docker exec veil-os veil-browser open https://example.com
docker exec veil-os veil-browser snapshot

# Sandbox works inside the container
docker exec veil-os python3 veil-sandbox/demo.py
```

Or, without `docker exec` (and the way the system is actually used):
drive the tasks from the Security Center — the researcher task opens
a real page through the CLI browser and saves findings via the
sandbox, and the video task cuts and renders a clip with ffmpeg.
Approve the browser navigation when it appears in pending approvals:

```bash
curl -s -X POST http://127.0.0.1:8765/api/task/start \
  -H 'Content-Type: application/json' -d '{"task":"researcher"}'
# ... approve the pending proposal when it shows up ...
curl -s http://127.0.0.1:8765/api/state | python3 -m json.tool | grep -A3 '"task"'

curl -s -X POST http://127.0.0.1:8765/api/task/start \
  -H 'Content-Type: application/json' -d '{"task":"video"}'
# ... wait, then check the task state the same way: it reports the render
```

## Honest limits (v1)

- **Root in the container.** Broker, agent runtime and desktop all run
  as root inside the container (see "Why root" above). The production
  split (separate `veil` / `veil-agent` users) is not in this image
  yet — same as the dev host, where everything runs as one UID and
  separation is socket + method allowlist only.
- **ffmpeg 5.1 in the image, and v1 audio is concatenated, not mixed.**
  Debian bookworm ships ffmpeg 5.1.9, whose `amix` with `adelay`'d
  inputs hangs (spins at 100% CPU, no output) when the inputs start
  at different times. Sequential v1 clips are therefore joined with
  audio `concat` (with `anullsrc` silence for gaps) instead of
  `amix`; `amix` is only used when a1/a2 overlay audio actually
  exists. Verified: the same graph renders on the host's ffmpeg
  8.1.2 either way, and the concat version renders in ~5s in the
  container. If overlay audio is used in the container, that path
  still goes through `amix` and should be re-tested there.
- **Resource limits are rlimits only.** cgroup limits are not enforced
  (see `veil-sandbox/limits.py: probe_cgroups` — the dev host has no
  cgroup controllers, and most Docker hosts need cgroup v2 configured for
  them to apply). The Security Center shows the probe result.
- **Plain HTTP inside.** The Remote API is HTTP; put it behind a TLS
  proxy or SSH tunnel before exposing it past localhost.
- **No faster-whisper in the image.** Transcribe fails clearly with an
  install hint (it never fakes a transcript). Melt/MLT is not installed
  either; ffmpeg is the v1 renderer and `.mlt` is export-only.
- **The image is the B-first deliverable.** The bootable Arch ISO
  (Stage 6) is a separate, later step — this image is how VEIL runs
  today, on any host with Docker.
