# VEIL Sandbox — Bubblewrap (v1)

Real isolation, not a prompt. The broker decides **if**; bwrap decides **where**.

## Run it

```bash
cd veil-os/veil-sandbox
python3 test_sandbox.py
python3 sandbox.py --workspace ./test-workspace -- ls /workspace
python3 sandbox.py --workspace ./test-workspace -- cat notes.md
```

## What's isolated

- New user / pid / ipc / net / uts namespaces, `--die-with-parent`
- `/usr` read-only, `/workspace` is the *only* host directory mounted
- `/home` empty tmpfs, `/tmp` tmpfs, `/etc` synthetic (passwd/group/hosts/resolv.conf only)
- Network: `--unshare-net`, loopback only. No DNS, no egress.
- Every command goes through the broker first (`terminal.run` approval). Denied = never executed.

## Verified bypass tests

`test_sandbox.py` proves:
- workspace read/write works
- `/home/hatch`, `MEMORY.md`, host `/etc`, `/etc/shadow` — invisible, not just denied
- write to `/usr` fails (read-only fs)
- `curl` blocked by broker; even approved `python3` gets no socket route (namespace)
- snapshot + rollback restores the workspace

## Honest limits

- **No `/proc` on this host.** The dev VM forbids proc mounts in a nested user namespace, so the sandbox retries without it. Python/bash/ls/cat/git work fine without `/proc`; tools that need it will fail inside — documented, not hidden.
- **Bubblewrap ≠ microVM.** For untrusted files, websites, or multi-tenant work, graduate to Firecracker (see `ARCHITECTURE.md`). bwrap is the light-task layer.
- **Seccomp is not a sandbox.** No seccomp filter is claimed as the boundary here. It's a future hardening layer.
- **Network is always off in v1.** `allow_network=True` raises an error until the broker proxy exists. We don't silently enable egress.
- **Broker is still in-process in the prototype.** Separating it into its own process/user is the next hardening step — the sandbox already assumes that separation in its design.

## Limits + seccomp (v2)

Defaults per run: CPU 30s, address space 512 MB, 512 processes,
100 MB max file, 256 open files, 30s timeout, 1 MB output cap.
See `limits.py` and `seccomp.py`.

```bash
python3 test_limits_seccomp.py   # 11/11 pass
```

Enforced here: RLIMIT_CPU/AS/FSIZE/NOFILE (preexec_fn), timeout,
seccomp (mount/ptrace/reboot/bpf/io_uring/etc. -> EPERM).
NOT enforced here: cgroup v2 — controllers are empty on this host,
systemd MemoryMax is silently ignored. That is a production
requirement, not a dev-host fact. See `limits.py: probe_cgroups()`.
