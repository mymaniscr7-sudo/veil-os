"""
veil-exec Landlock layer — per-process capability restrictions.

Landlock lets an unprivileged process restrict its OWN ambient rights.
It is NOT a sandbox by itself and NOT domain policy (it understands
ports, not github.com) — it is one layer, under Bubblewrap, under the
broker. See ARCHITECTURE.md.

VERIFIED on this host (2026-11-05, kernel 7.0.0-43-generic):
  - landlock_create_ruleset returns a fd, restrict_self succeeds
  - in-process: workspace read works, /etc/passwd and ~/MEMORY.md
    are blocked with EACCES
  - see test_landlock.py — that is the proven path

NOT VERIFIED / NOT USED in v1:
  - exec-after-restrict is unreliable on this host. A process that
    calls restrict_self and then execs a new binary (ls, python3)
    fails with EACCES even when /usr, /lib, /lib64, /bin, /sbin are
    allowed — likely a loader / handled-access interaction on this
    nested host. We do NOT wire Landlock as the bwrap command runner
    until that is understood. Bubblewrap is the exec boundary in v1;
    Landlock is for in-process low-risk adapters (see below).
  - Do not claim Landlock alone is a sandbox. It isn't.

Usage as a library (in-process adapter):
  from landlock import apply_landlock
  apply_landlock([("/workspace", "rwx"), ("/usr", "rx")])
  # now this process can only read/write those paths for FS access

Usage as a demo:
  python3 landlock.py
  # applies Landlock in-process, then shows workspace read OK,
  # /etc/passwd and ~/MEMORY.md BLOCKED.
"""
from __future__ import annotations

import ctypes
import os
import sys

libc = ctypes.CDLL("libc.so.6", use_errno=True)

LANDLOCK_CREATE_RULESET = 444
LANDLOCK_ADD_RULE = 445
LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_RULE_PATH_BENEATH = 1

EXECUTE = 1 << 0
READ_FILE = 1 << 1
WRITE_FILE = 1 << 2
READ_DIR = 1 << 3
REMOVE = 1 << 4
MAKE_CHAR = 1 << 5

# Handled set matching the pattern verified working in-process here:
# bits 0-5. See test_landlock.py.
HANDLED_FS = EXECUTE | READ_FILE | WRITE_FILE | READ_DIR | REMOVE | MAKE_CHAR


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("parent_fd", ctypes.c_int)]


def _parse_mode(s: str) -> int:
    m = 0
    if "r" in s:
        m |= READ_FILE | READ_DIR
    if "w" in s:
        m |= WRITE_FILE | REMOVE
    if "x" in s:
        m |= EXECUTE
    return m


def apply_landlock(allow: list[tuple[str, str]]):
    attr = RulesetAttr(HANDLED_FS, 0, 0)
    fd = libc.syscall(LANDLOCK_CREATE_RULESET, ctypes.byref(attr),
                      ctypes.sizeof(attr), 0)
    if fd < 0:
        e = ctypes.get_errno()
        raise OSError(e, f"landlock_create_ruleset failed: {os.strerror(e)}")
    for path, mode in allow:
        try:
            pfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        except OSError as e:
            raise OSError(f"landlock: cannot open {path}: {e}") from e
        a = PathBeneathAttr(_parse_mode(mode), pfd)
        r = libc.syscall(LANDLOCK_ADD_RULE, fd, LANDLOCK_RULE_PATH_BENEATH,
                         ctypes.byref(a), 0)
        os.close(pfd)
        if r != 0:
            e = ctypes.get_errno()
            raise OSError(e, f"landlock_add_rule {path} failed: {os.strerror(e)}")
    r = libc.syscall(LANDLOCK_RESTRICT_SELF, fd, 0)
    if r != 0:
        e = ctypes.get_errno()
        raise OSError(e, f"landlock_restrict_self failed: {os.strerror(e)}")


def main():
    ws = "/home/hatch/workspace/veil-os/veil-sandbox/test-workspace"
    print("Landlock in-process demo — restrict, then try reads")
    apply_landlock([(ws, "rwx"), ("/usr", "rx"), ("/tmp", "rwx")])
    for p in [f"{ws}/notes.md", "/etc/passwd", "/home/hatch/MEMORY.md"]:
        try:
            print(f"  {p} -> OK: {open(p).read()[:30].strip()!r}")
        except Exception as e:
            print(f"  {p} -> BLOCKED: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
