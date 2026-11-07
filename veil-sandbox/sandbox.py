"""
VEIL Sandbox — real Bubblewrap isolation (Stage 2, v1)

What it does, in one sentence:
  the broker decides IF a command may run, bwrap decides WHERE it runs.

Isolation (verified on this host, Ubuntu 24.04, bwrap 0.9.0):
  - new user / pid / ipc / net / uts namespaces (--unshare-*)
  - --die-with-parent, hostname veil-sandbox
  - /usr read-only, /bin /lib /lib64 /sbin as symlinks to usr/*
  - /workspace = ONLY host directory mounted (the task workspace)
  - /home = empty tmpfs, /tmp = tmpfs, /etc = minimal synthetic files
    (passwd/group/hosts/resolv.conf — NO host /etc, no host users,
     no host credentials, no /etc/shadow)
  - /dev = fresh devtmpfs, /proc = attempted, gracefully skipped on
    hosts that forbid proc mounts in a nested user namespace
    (this dev host does — documented, not hidden)
  - network: --unshare-net, loopback only. curl/ping to the outside
    fails. Broker-only network (proxy) is Phase 2, not faked here.

What it does NOT do (honest limits, from the research):
  - seccomp is NOT a sandbox by itself — we don't claim it is.
    It is one layer (see seccomp.py). A seccomp filter is now
    installed inside bwrap by default, blocking mount/ptrace/reboot/
    kexec/module-load/bpf/io_uring/time-setting etc. with EPERM.
  - cgroup v2 is NOT enforced on this dev host: controllers are
    empty, memory.max/cpu.max/pids.max don't exist, systemd
    MemoryMax is silently ignored (see limits.py). Resource limits
    that ARE enforced are rlimits (CPU/AS/FSIZE/NPROC/NOFILE) via
    preexec_fn + subprocess timeout. Do not claim cgroup enforcement
    here — it's a production requirement.
  - This is Bubblewrap, not a microVM. For untrusted files/websites/
    multi-tenant work, graduate to Firecracker (see ARCHITECTURE.md).
  - No /proc on this host means tools that require /proc may fail
    inside the sandbox. Python, bash, ls, cat, git work without it.

Broker gating (v2 — separate process):
  run() first asks veil-broker over its agent socket (broker_client).
  If the broker denies, or is unreachable, nothing is executed —
  no bwrap call at all. No in-process fallback: falling back would
  defeat the separation. broker_mode="inprocess" exists only as an
  explicit opt-in for offline unit tests; it is NOT the default and
  must never be used in production.
  Every run is audited by the broker, and snapshots give rollback:
  propose -> validate (broker) -> execute (bwrap) -> observe -> rollback
  (research tool contract, adapted to VEIL).

Usage:
  python3 sandbox.py --workspace ./test-workspace -- ls /workspace
  python3 test_sandbox.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "veil-core"))

from broker_client import BrokerClient, BrokerUnavailable  # noqa: E402
from limits import ResourceLimits, make_preexec  # noqa: E402

ETC_MINIMAL = HERE / "etc-minimal"
SECCOMP_PY = HERE / "seccomp.py"
SNAPSHOT_DIR = HERE / ".snapshots"


@dataclass
class ServiceDecision:
    allowed: bool
    reason: str
    action: str = "terminal.run"
    outcome: str = ""


class SandboxDenied(Exception):
    pass


class VeilSandbox:
    def __init__(self, workspace: Path, policy_path: Path | None = None,
                 allow_network: bool = False,
                 broker_mode: str = "service",
                 broker_run_dir: Path | None = None,
                 session: str | None = None,
                 limits: ResourceLimits | None = None,
                 seccomp: bool = True):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.session = session or f"task-{self.workspace.name}"
        self.broker_mode = broker_mode
        self.limits = limits or ResourceLimits()
        self.seccomp = seccomp
        if broker_mode == "service":
            # Thin RPC client only — no policy file, no broker code.
            self.broker_client = BrokerClient(run_dir=broker_run_dir) if broker_run_dir else BrokerClient()
            self.broker = None
        elif broker_mode == "inprocess":
            # Offline tests only. Explicit opt-in, never the default.
            from broker import Broker  # noqa: E402
            if policy_path is None:
                policy_path = HERE.parent / "veil-core" / "policy.example.json"
            self.broker = Broker(policy_path)
            self.broker_client = None
        else:
            raise ValueError(f"unknown broker_mode: {broker_mode!r}")
        # v1: network is always off. allow_network=True is refused
        # until the broker proxy exists — never silently enable it.
        if allow_network:
            raise ValueError(
                "allow_network=True is not supported in sandbox v1: "
                "broker-only network via proxy is Phase 2. "
                "Use allow_network=False (default)."
            )
        self.allow_network = False

    # -- bwrap command -------------------------------------------------
    def _bwrap_cmd(self, command: list[str]) -> list[str]:
        links = []
        for src, dst in [("usr/bin", "/bin"), ("usr/lib", "/lib"),
                         ("usr/lib64", "/lib64"), ("usr/sbin", "/sbin")]:
            links += ["--symlink", src, dst]
        cmd = [
            "bwrap",
            "--unshare-user", "--unshare-pid", "--unshare-ipc",
            "--unshare-net", "--unshare-uts",
            "--die-with-parent",
            "--hostname", "veil-sandbox",
            "--ro-bind", "/usr", "/usr",
            *links,
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--tmpfs", "/home",
            "--tmpfs", "/etc",
            "--ro-bind", str(ETC_MINIMAL / "passwd"), "/etc/passwd",
            "--ro-bind", str(ETC_MINIMAL / "group"), "/etc/group",
            "--ro-bind", str(ETC_MINIMAL / "hosts"), "/etc/hosts",
            "--ro-bind", str(ETC_MINIMAL / "resolv.conf"), "/etc/resolv.conf",
            "--bind", str(self.workspace), "/workspace",
            "--chdir", "/workspace",
        ]
        # Dynamic linker cache: the inner interpreter may live in
        # /usr/local (python:3.12-slim image) with its libs in
        # /usr/local/lib, which the linker only finds via ld.so.cache.
        # /etc is a fresh tmpfs here, so bind the cache in (when the
        # host has one). Without it the inner python3 fails with
        # "libpython3.12.so.1.0: cannot open shared object file".
        for _ld in ("/etc/ld.so.cache", "/etc/ld.so.conf"):
            if Path(_ld).exists():
                cmd += ["--ro-bind", _ld, _ld]
        if self.seccomp:
            # Bind the seccomp wrapper in read-only, so the inner
            # command runs as: python3 /veil/seccomp.py -- bash -c …
            cmd += ["--ro-bind", str(SECCOMP_PY), "/veil/seccomp.py"]
        # /proc: try it, fall back cleanly if the host forbids it.
        # (This host does forbid it in a nested user ns.)
        cmd += ["--proc", "/proc"]
        cmd += command
        return cmd

    def _run_bwrap(self, command: list[str], timeout: int | None = None):
        if timeout is None:
            timeout = self.limits.timeout_seconds
        cmd = self._bwrap_cmd(command)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout,
                               preexec_fn=make_preexec(self.limits))
        except subprocess.TimeoutExpired as e:
            # Timeout is enforced: the run is killed, not left running.
            return subprocess.CompletedProcess(
                cmd, -1,
                stdout=(e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=f"timeout: killed after {timeout}s (limit)\n")
        if r.returncode == 1 and "Can't mount proc" in r.stderr:
            # Retry without --proc — same isolation otherwise.
            cmd2 = [c for c in cmd if c != "--proc"]
            cmd2 = [c for c in cmd2 if c != "/proc"]
            try:
                r = subprocess.run(cmd2, capture_output=True, text=True,
                                   timeout=timeout,
                                   preexec_fn=make_preexec(self.limits))
            except subprocess.TimeoutExpired as e:
                return subprocess.CompletedProcess(
                    cmd2, -1, stdout="",
                    stderr=f"timeout: killed after {timeout}s (limit)\n")
        # Output cap: don't let a run flood the caller with output.
        cap = self.limits.max_output_bytes
        if len(r.stdout or "") > cap:
            r = subprocess.CompletedProcess(
                r.args, r.returncode,
                stdout=(r.stdout or "")[:cap] + "\n[output truncated]\n",
                stderr=r.stderr)
        return r

    # -- snapshots / rollback ------------------------------------------
    def snapshot(self) -> str:
        snap_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
        dest = SNAPSHOT_DIR / snap_id
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.workspace, dest, dirs_exist_ok=True)
        # Record origin so the Security Center can roll back to the
        # right workspace (and show it).
        try:
            (dest / ".veil-meta.json").write_text(json.dumps({
                "snapshot_id": snap_id,
                "workspace": str(self.workspace),
                "created_at": snap_id.split("-")[0],
            }))
        except OSError:
            pass
        return snap_id

    def rollback(self, snap_id: str) -> None:
        src = SNAPSHOT_DIR / snap_id
        if not src.exists():
            raise ValueError(f"unknown snapshot: {snap_id}")
        shutil.rmtree(self.workspace)
        shutil.copytree(src, self.workspace)
        # .veil-meta.json records the snapshot's origin; it is not
        # part of the workspace and must not leak into a rollback.
        meta = self.workspace / ".veil-meta.json"
        try:
            meta.unlink()
        except FileNotFoundError:
            pass

    # -- the only entry point ------------------------------------------
    def _decide(self, command: str, approval_token: str | None = None) -> ServiceDecision:
        """Ask the broker. Fail closed on deny AND on broker-down."""
        if self.broker_mode == "inprocess":
            d = self.broker.request("terminal.run", command=command)
            return ServiceDecision(d.allowed, d.reason, "terminal.run",
                                   "allow" if d.allowed else "deny")
        first = command.strip().split()[0] if command.strip() else ""
        try:
            r = self.broker_client.propose(
                action="terminal.run", target=command,
                expected_effect=f"run approved command {first} in /workspace",
                capability={
                    "capability": "terminal.run",
                    "who": f"agent:{self.session}",
                    "scope": {"commands": [first], "workspace": str(self.workspace)},
                    "session": self.session, "risk": "medium",
                },
                approval_token=approval_token)
        except BrokerUnavailable as e:
            # Fail closed: broker down = no execution, no fallback.
            return ServiceDecision(False, f"fail-closed: broker unavailable: {e}",
                                   "terminal.run", "deny")
        outcome = r.get("outcome", "deny")
        return ServiceDecision(outcome == "allow",
                               f"{outcome}: {r.get('reason', '')}",
                               "terminal.run", outcome)

    def run(self, command: str, timeout: int | None = None, snapshot_first: bool = True,
            approval_token: str | None = None,
            limits: ResourceLimits | None = None):
        """
        command: a single shell string, e.g. "ls /workspace".
        Returns (decision, result, snapshot_id).
        Broker deny / broker down -> no execution, result is None.
        Limits + seccomp are applied to every run (see limits.py).
        """
        if limits is not None:
            self.limits = limits
        decision = self._decide(command, approval_token=approval_token)
        if not decision.allowed:
            return decision, None, None
        snap_id = self.snapshot() if snapshot_first else None
        # Shell is inside the sandbox; the broker already approved
        # the first word. Keep the inner shell minimal.
        # Seccomp wrapper installs the filter inside bwrap, then execs bash.
        # Resolve the interpreters from PATH: the image's Python lives
        # at /usr/local/bin/python3 (python:3.12-slim), not /usr/bin.
        # /usr is ro-bound into the sandbox, so a PATH-resolved binary
        # under /usr stays valid inside it.
        python = shutil.which("python3") or "/usr/bin/python3"
        bash = shutil.which("bash") or "/usr/bin/bash"
        if self.seccomp:
            inner = [python, "/veil/seccomp.py", "--",
                     bash, "-c", command]
        else:
            inner = [bash, "-c", command]
        result = self._run_bwrap(inner, timeout=timeout)
        return decision, result, snap_id


def main():
    import argparse
    p = argparse.ArgumentParser(description="VEIL Sandbox — run a broker-approved command in bwrap")
    p.add_argument("--workspace", default=str(HERE / "test-workspace"))
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args()
    cmd = [c for c in args.command if c != "--"]
    if not cmd:
        print("usage: python3 sandbox.py --workspace ./test-workspace -- ls /workspace")
        return 2
    sb = VeilSandbox(Path(args.workspace))
    decision, result, snap = sb.run(" ".join(cmd))
    print(f"broker: {'ALLOWED' if decision.allowed else 'DENIED'}: {decision.reason}")
    if "broker unavailable" in decision.reason:
        print("hint: start the broker first: python3 ../veil-core/veil-broker.py start")
    if result is None:
        return 1
    print(f"snapshot: {snap}  (rollback with sb.rollback('{snap}'))")
    print(f"exit: {result.returncode}")
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print("stderr:", result.stderr, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
