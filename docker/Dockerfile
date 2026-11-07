# syntax=docker/dockerfile:1
# VEIL OS — Docker image
#
# Builds the whole stack into one runnable image:
#   VEIL Permission Broker + Sandbox (Bubblewrap) + Security Center (:8765)
#   + VEIL Video remote API (:8766) + veil-browser-cli + Chrome for Testing
#   + ffmpeg (video rendering / probing)
#
# Build from the workspace root — the context must contain BOTH repos,
# side by side, because the image vendors the browser CLI from source:
#
#   cd <workspace>            # contains veil-os/ and veil-browser-cli/
#   docker build -f veil-os/docker/Dockerfile -t veil-os:latest .
#
# Or with compose (does the same thing):  cd veil-os/docker && docker compose up --build
#
# Run (Bubblewrap needs user-namespace + mount syscalls the default Docker
# seccomp profile blocks, so the container needs these two flags):
#
#   docker run -d --name veil-os \
#     --cap-add=SYS_ADMIN --security-opt seccomp=unconfined \
#     -p 127.0.0.1:8765:8765 -p 127.0.0.1:8766:8766 \
#     -v veil-workspace:/workspace -v veil-run:/opt/veil/run \
#     -v veil-video-projects:/opt/veil/veil-video/projects \
#     veil-os:latest
#
#   Security Center: http://127.0.0.1:8765/   (no auth by design — localhost only)
#   Remote API:      http://127.0.0.1:8766/   (Bearer token, printed once at first boot)
#
# Honest limits (v1):
# - The broker, agent runtime and desktop all run as root in the
#   container (Bubblewrap needs uid 0 here — see "Why root" in
#   docker/README.md). The production split (separate `veil` /
#   `veil-agent` users, cgroup enforcement) is not in this image yet.
# - Resource limits are rlimits only; cgroup limits are not enforced
#   (same as on the dev host — see veil-sandbox/limits.py probe_cgroups).
# - The Remote API is plain HTTP; put it behind a TLS proxy / SSH tunnel
#   before exposing it past localhost.

# Build-time proxy (optional): pass --build-arg HTTP_PROXY=... when the
# build host needs a proxy (see docker/README.md). Declared per stage so
# RUN steps (apt, cargo, Chrome download) can see them; not persisted
# into the final image config.
#
# HATCH_CA_B64 (optional): base64 of an extra CA certificate, for build
# hosts behind a TLS-MITM egress proxy (e.g. this dev host's Hatch
# Sandbox Egress CA). Pass it like:
#   --build-arg HATCH_CA_B64="$(base64 -w0 /etc/ssl/certs/hatch-egress-ca.pem)"
# Empty in CI (no proxy) — the install step is a no-op there. A CA is a
# public trust anchor, not a secret, but it is host-specific, so it is
# only baked in when you pass it.
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG no_proxy
ARG HATCH_CA_B64=""

# ---------- stage 1: build the veil-browser CLI (Rust) ----------
FROM rust:1-bookworm AS browser-builder

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG no_proxy
ARG HATCH_CA_B64=""

# Trust the build host's extra CA first (no-op when HATCH_CA_B64 is empty).
RUN if [ -n "$HATCH_CA_B64" ]; then \
      echo "$HATCH_CA_B64" | base64 -d > /usr/local/share/ca-certificates/hatch-egress-ca.crt && \
      update-ca-certificates; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends \
      pkg-config libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/veil-browser-cli
COPY veil-browser-cli/Cargo.toml veil-browser-cli/Cargo.lock ./
COPY veil-browser-cli/src ./src
RUN cargo build --release --locked
# -> /src/veil-browser-cli/target/release/veil-browser

# ---------- stage 2: runtime ----------
FROM python:3.12-slim-bookworm

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ARG http_proxy
ARG https_proxy
ARG no_proxy
ARG HATCH_CA_B64=""

LABEL org.opencontainers.image.title="VEIL OS"
LABEL org.opencontainers.image.description="AI-native operating environment: permission broker, sandbox, CLI browser, video editor, Security Center"
LABEL org.opencontainers.image.source="https://github.com/mymaniscr7-sudo/veil-os"

ENV PYTHONUNBUFFERED=1 \
    VEIL_HOME=/opt/veil \
    VEIL_BROWSER_HOME=/home/veil/.cache/veil-browser \
    VEIL_BROWSER_CHROME=/home/veil/.cache/veil-browser/chrome-linux64/chrome

# VEIL_BROWSER_CHROME is set explicitly (not HOME-derived): the runtime
# runs as root, whose HOME is /root, but Chrome was installed into
# /home/veil/.cache by install-chrome.sh. Without this the daemon
# fails with "no Chrome/Chromium binary found".

RUN apt-get update && apt-get install -y --no-install-recommends \
      # video: render + probe
      ffmpeg fonts-dejavu-core libfontconfig1 \
      # sandbox: Bubblewrap + the libseccomp the seccomp wrapper needs
      bubblewrap libseccomp2 \
      # runtime plumbing
      curl ca-certificates unzip tini procps \
      # Chrome headless deps
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
      libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
      libpango-1.0-0 libcairo2 libatspi2.0-0 libx11-6 libxcb1 libxext6 libxi6 libxtst6 \
      # certutil: the browser CLI imports an egress-proxy CA into Chrome's
      # per-session NSS database with it. Without it, Chrome shows a
      # privacy error on every MITM-proxied site and the CLI can't fix it.
      libnss3-tools \
    && rm -rf /var/lib/apt/lists/*

# Bubblewrap must be setuid root. Debian installs it with file
# capabilities instead, but those do not survive this image's build
# (nested user namespaces strip them from the layer) and at runtime the
# veil user then gets: "bwrap: Unexpected capabilities but not setuid".
# setuid root gives bwrap the namespace caps it needs; it still drops
# to the invoking user for the inner command. The container is the
# sandbox boundary; the broker still gates every action.
RUN chmod u+s /usr/bin/bwrap

# Trust the build host's extra CA (no-op when HATCH_CA_B64 is empty).
# Must run after the apt install above, which provides update-ca-certificates.
RUN if [ -n "$HATCH_CA_B64" ]; then \
      echo "$HATCH_CA_B64" | base64 -d > /usr/local/share/ca-certificates/hatch-egress-ca.crt && \
      update-ca-certificates; \
    fi

RUN useradd -m -u 1000 -s /bin/bash veil \
    && mkdir -p /opt/veil /workspace \
    && chown -R veil:veil /opt/veil /workspace

# VEIL OS source (broker, sandbox, desktop, video, environments, docs).
COPY --chown=veil:veil veil-os /opt/veil

# Bubblewrap's inner root runs in a nested user namespace, where its
# CAP_DAC_OVERRIDE does NOT reach files owned by the outer namespace.
# The COPY above leaves /opt/veil owned by veil (0700-ish dirs, 0660
# files), so the sandbox's --ro-bind of etc-minimal/* fails with
# "Permission denied" even though the outer process is root. Make the
# source world-readable/traversable; secrets live in run/ (a volume),
# not in /opt/veil.
RUN chmod -R a+rX /opt/veil

# veil-browser CLI binary, built from source in stage 1.
COPY --from=browser-builder --chown=veil:veil \
    /src/veil-browser-cli/target/release/veil-browser /usr/local/bin/veil-browser

# Chrome for Testing (stable) — the CLI auto-discovers it in
# $VEIL_BROWSER_HOME; or set VEIL_BROWSER_CHROME to any Chrome binary.
# Installed as `veil` (it needs a real home dir), but the runtime runs
# as root: Bubblewrap 0.8.0 only works setuid-root or as uid 0, and this
# image's rootfs is mounted nosuid (so setuid bwrap is ignored) while a
# non-root user can't write bwrap's two-line uid_map. As uid 0 bwrap
# inherits the container's caps and the sandbox works. The container
# itself is the isolation boundary from the host; the broker still
# gates every action. (See docker/README.md "Why root".)
USER veil
COPY --chown=veil:veil veil-browser-cli/scripts/install-chrome.sh /tmp/install-chrome.sh
RUN bash /tmp/install-chrome.sh && rm /tmp/install-chrome.sh \
    && /home/veil/.cache/veil-browser/chrome-linux64/chrome --version

USER root

WORKDIR /opt/veil

# Entrypoint: broker + Security Center + video remote API.
RUN chmod +x /opt/veil/docker/entrypoint.sh

# /workspace: agent task workspaces. /opt/veil/run: broker sockets, pids,
# remote-token. veil-video/projects: video projects + renders.
VOLUME ["/workspace", "/opt/veil/run", "/opt/veil/veil-video/projects"]

EXPOSE 8765 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/api/state || exit 1

ENTRYPOINT ["tini", "--", "/opt/veil/docker/entrypoint.sh"]
