"""
veil-exec resource limits — what is actually enforced on this host.

Honest status (probed 2026-11-05, Ubuntu 24.04, kernel 7.0.0-43):
  - cgroup v2: NOT usable here. /sys/fs/cgroup/cgroup.controllers is
    empty, there is no memory.max / cpu.max / pids.max, direct writes
    fail Permission denied, and systemd-run --scope --property=MemoryMax=50M
    is silently ignored (systemctl show reports MemoryMax=infinity,
    and a 200 MB allocation under that scope succeeds).
    Do NOT claim cgroup enforcement on this host. It is a production
    requirement (Arch host with controllers enabled), not a dev-host fact.
  - rlimits (kernel, per-process, inherited across exec): ENFORCED.
    RLIMIT_CPU, RLIMIT_AS, RLIMIT_FSIZE, RLIMIT_NOFILE, RLIMIT_CORE.
    RLIMIT_NPROC is enforced but counts per-UID, not per-sandbox —
    the sandbox shares UID 0 with the host, so the default is loose
    (512) to avoid breaking normal work. Real per-sandbox process
    limits need cgroup pids.max or a dedicated UID per sandbox.
  - timeout (subprocess): ENFORCED. A run that exceeds it is killed.
  - output cap: ENFORCED in the caller (sandbox truncates).

RLIMIT_AS is address space, NOT cgroup memory.max. Python needs
~100-200 MB of virtual address space just to start, so a 512 MB
default is roomy for normal work and still stops a 2 GB allocation
attempt with MemoryError. Do not set it to 50 MB and expect Python
to run.

Defaults (per task, via the broker policy in future):
  cpu_seconds=30, memory_mb=512, max_processes=512, max_file_mb=100,
  max_open_files=256, timeout_seconds=30
"""
from __future__ import annotations

import resource
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ResourceLimits:
    cpu_seconds: int = 30          # RLIMIT_CPU: SIGXCPU then SIGKILL
    memory_mb: int = 512           # RLIMIT_AS: virtual address space
    max_processes: int = 512       # RLIMIT_NPROC: per-UID, see above
    max_file_mb: int = 100         # RLIMIT_FSIZE: file writes cap
    max_open_files: int = 256      # RLIMIT_NOFILE
    timeout_seconds: int = 30      # subprocess timeout (wall clock)
    max_output_bytes: int = 1_000_000


def _exists(p: Path) -> bool:
    """Path.exists() lets PermissionError escape on restricted mounts
    (e.g. a container's read-only /sys/fs/cgroup, where stat on
    memory.max raises EACCES instead of returning False). Any stat
    failure means 'not usable here'."""
    try:
        return p.exists()
    except OSError:
        return False


def probe_cgroups() -> dict:
    """Probe, don't assume. Returns what the host actually offers."""
    cg = Path("/sys/fs/cgroup")
    controllers = []
    try:
        controllers = (cg / "cgroup.controllers").read_text().split()
    except OSError:
        pass
    return {
        "cgroup2_mounted": cg.is_dir(),
        "controllers": controllers,
        "memory_max_exists": _exists(cg / "memory.max"),
        "cpu_max_exists": _exists(cg / "cpu.max"),
        "pids_max_exists": _exists(cg / "pids.max"),
        "enforced_here": bool(controllers),
    }


def make_preexec(limits: ResourceLimits):
    """Return a preexec_fn for subprocess: set rlimits in the child
    before it execs bwrap. Limits are inherited into the sandbox."""
    def preexec():
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        mem = limits.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE,
                           (limits.max_file_mb * 1024 * 1024,
                            limits.max_file_mb * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NPROC,
                           (limits.max_processes, limits.max_processes))
        resource.setrlimit(resource.RLIMIT_NOFILE,
                           (limits.max_open_files, limits.max_open_files))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return preexec


def describe() -> str:
    probe = probe_cgroups()
    return (
        f"cgroup v2: mounted={probe['cgroup2_mounted']} "
        f"controllers={probe['controllers'] or 'NONE — not enforced here'}; "
        f"rlimits: CPU/AS/FSIZE/NPROC/NOFILE enforced via preexec_fn; "
        f"timeout: enforced via subprocess"
    )
