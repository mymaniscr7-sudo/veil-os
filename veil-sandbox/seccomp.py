"""
veil-exec seccomp filter — reduce the syscall surface inside the sandbox.

Built with libseccomp.so.2 via ctypes (no python-seccomp package, no
libseccomp-dev header on this host — the .so exports the symbols and
ctypes calls them directly). Default action is ALLOW; the list below
is DENIED with EPERM (errno 1).

Verified on this host (2026-11-05): filter loads, mount/ptrace/reboot
return -1 EPERM, normal open/getpid still work.

What is blocked (x86_64 numbers from unistd_64.h):
  mount 165, umount2 166, pivot_root 155, chroot 161 — filesystem escape
  reboot 169, kexec_load 246, kexec_file_load 320 — reboot / kernel load
  init_module 175, finit_module 313, delete_module 176 — kernel modules
  ptrace 101, process_vm_readv 310, process_vm_writev 311 — cross-process
  perf_event_open 298, bpf 321 — kernel introspection / BPF
  keyctl 250, add_key 248, request_key 249 — kernel keyring
  io_uring_setup 425, io_uring_enter 426, io_uring_register 427 — async I/O
    that can bypass some restrictions
  clock_settime 227, settimeofday 164, adjtimex 159 — host clock
  swapon 167, swapoff 168, fanotify_init 300 — host devices/filesystems
  open_tree 428, move_mount 429, fsopen 430, fsconfig 431, fsmount 432,
  mount_setattr 442 — mount API family

What is NOT blocked (deliberately, v1):
  unshare 272, setns 308, clone3 435 — blocking these breaks some
  runtimes and is a future tightening, not a v1 claim. Bubblewrap
  already did the namespacing; the agent shouldn't need these, but
  we don't pretend to block them until we've tested the breakage.

Seccomp is NOT a sandbox by itself (kernel docs). It is one layer:
broker (if) + Bubblewrap (where) + Landlock (per-process FS) +
seccomp (syscall surface) + rlimits (resources). No single layer
is the security boundary.

Usage as a wrapper (runs INSIDE bwrap, after namespaces are set up):
  python3 seccomp.py -- <command...>
  # installs the filter in this process, then execs the command.
  # The filter is inherited by children across fork/exec.
"""
from __future__ import annotations

import ctypes
import os
import sys

# x86_64 syscall numbers (unistd_64.h)
BLOCKED = {
    165: "mount", 166: "umount2", 155: "pivot_root", 161: "chroot",
    169: "reboot", 246: "kexec_load", 320: "kexec_file_load",
    175: "init_module", 313: "finit_module", 176: "delete_module",
    101: "ptrace", 310: "process_vm_readv", 311: "process_vm_writev",
    298: "perf_event_open", 321: "bpf",
    250: "keyctl", 248: "add_key", 249: "request_key",
    425: "io_uring_setup", 426: "io_uring_enter", 427: "io_uring_register",
    227: "clock_settime", 164: "settimeofday", 159: "adjtimex",
    167: "swapon", 168: "swapoff", 300: "fanotify_init",
    428: "open_tree", 429: "move_mount", 430: "fsopen",
    431: "fsconfig", 432: "fsmount", 442: "mount_setattr",
}

SCMP_ACT_ALLOW = 0x7FFF0000


def _scmp_act_errno(e: int) -> int:
    return 0x00050000 | e


def install_filter() -> None:
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(SCMP_ACT_ALLOW)
    if not ctx:
        raise OSError("seccomp_init failed")
    for nr in BLOCKED:
        r = lib.seccomp_rule_add(ctx, _scmp_act_errno(1), nr, 0)
        if r != 0:
            raise OSError(f"seccomp_rule_add {nr} ({BLOCKED[nr]}) failed: {r}")
    r = lib.seccomp_load(ctx)
    if r != 0:
        raise OSError(f"seccomp_load failed: {r}")


def main(argv) -> int:
    # argv: [--] <command...>
    cmd = [a for a in argv if a != "--"]
    if not cmd:
        print("usage: python3 seccomp.py -- <command...>", file=sys.stderr)
        return 2
    try:
        install_filter()
    except OSError as e:
        print(f"seccomp: fail-closed: {e}", file=sys.stderr)
        return 1
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
