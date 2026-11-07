#!/usr/bin/env python3
"""veil-broker CLI — start / stop / status / grant / propose.

  python3 veil-broker.py start [--policy …] [--run-dir …]
  python3 veil-broker.py stop
  python3 veil-broker.py status
  python3 veil-broker.py grant --capability browser.navigate --domains github.com --session task-1
  python3 veil-broker.py propose --action filesystem.write --target /workspace/x.py

start: spawns broker_service.py as a detached process, waits for the
agent socket, prints pid. stop: SIGTERM to the pid in run/veil-broker.pid.
grant/propose are thin wrappers over the admin/agent clients — grant
is the Security Center path, propose is the agent path.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
RUN_DIR = HERE.parent / "run"
POLICY = HERE / "policy_v2.example.json"

sys.path.insert(0, str(HERE))
from broker_client import AdminClient, BrokerClient, BrokerUnavailable  # noqa: E402


def _pid_file(run_dir: Path) -> Path:
    return Path(run_dir) / "veil-broker.pid"


def cmd_start(args):
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    pid_file = _pid_file(run_dir)
    if pid_file.exists():
        # Trust the socket, not the pid. PIDs are namespaced per
        # container, so a pid file from a previous container (or a
        # previous boot, with the run dir on a persisted volume) can
        # name a live but unrelated process — including this very
        # start command. If the broker doesn't answer a ping, the
        # pid file and sockets are stale: clear them and start fresh.
        try:
            BrokerClient(run_dir=run_dir).ping()
            print(f"broker already running pid={pid_file.read_text().strip()}")
            return 0
        except (BrokerUnavailable, ValueError, OSError):
            pid_file.unlink(missing_ok=True)
            for name in ("veil-broker-agent.sock", "veil-broker-admin.sock"):
                (run_dir / name).unlink(missing_ok=True)
    log = open(run_dir / "veil-broker.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "broker_service.py"), "--serve",
         "--policy", str(args.policy), "--run-dir", str(run_dir)],
        stdout=log, stderr=log, start_new_session=True, close_fds=True)
    # Wait for the agent socket (up to 5s).
    agent_sock = run_dir / "veil-broker-agent.sock"
    for _ in range(50):
        if agent_sock.exists():
            break
        time.sleep(0.1)
    else:
        print("broker did not start — see run/veil-broker.log")
        return 1
    print(f"broker started pid={proc.pid} agent={agent_sock}")
    print(f"  admin={run_dir / 'veil-broker-admin.sock'} (0600)")
    return 0


def cmd_stop(args):
    run_dir = Path(args.run_dir)
    pid_file = _pid_file(run_dir)
    if not pid_file.exists():
        print("broker is not running (no pid file)")
        # Still try admin shutdown in case pid file was lost.
        return 0
    # Prefer the admin socket's shutdown: it talks to the broker itself.
    # SIGTERM to the pid file's pid is the fallback, and only after the
    # broker has answered a ping — a stale pid file must never SIGTERM
    # an unrelated process that reused the pid (see cmd_start).
    try:
        AdminClient(run_dir=run_dir).shutdown()
        print("broker stopped via admin socket")
        pid_file.unlink(missing_ok=True)
        return 0
    except (BrokerUnavailable, OSError):
        pass
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        pid_file.unlink(missing_ok=True)
        return 0
    try:
        BrokerClient(run_dir=run_dir).ping()
    except (BrokerUnavailable, OSError):
        print(f"broker not responding — clearing stale pid file (pid {pid})")
        pid_file.unlink(missing_ok=True)
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"pid {pid} already gone")
        pid_file.unlink(missing_ok=True)
        return 0
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    print(f"broker stopped pid={pid}")
    return 0


def cmd_status(args):
    run_dir = Path(args.run_dir)
    try:
        admin = AdminClient(run_dir=run_dir)
        print(json.dumps(admin.status(), indent=2))
        agent = BrokerClient(run_dir=run_dir)
        print("agent ping:", agent.ping())
        return 0
    except BrokerUnavailable as e:
        print(f"broker unavailable: {e}")
        return 1


def _cap_from_args(args) -> dict:
    scope: dict = {}
    if getattr(args, "domains", None):
        scope["domains"] = args.domains
    if getattr(args, "paths", None):
        scope["paths"] = args.paths
    if getattr(args, "commands", None):
        scope["commands"] = args.commands
    return {
        "capability": args.capability,
        "who": f"agent:{args.session}",
        "scope": scope,
        "session": args.session,
        "risk": getattr(args, "risk", "medium"),
    }


def cmd_grant(args):
    try:
        admin = AdminClient(run_dir=Path(args.run_dir))
        r = admin.grant_approval(_cap_from_args(args))
        print(f"approval token={r['token']} binding_id={r['binding_id']}")
        print("Give this token to the agent for ONE exact action — it does not transfer.")
        return 0
    except BrokerUnavailable as e:
        print(f"broker unavailable: {e}")
        return 1


def cmd_propose(args):
    try:
        client = BrokerClient(run_dir=Path(args.run_dir))
        r = client.propose(
            action=args.action, target=args.target,
            expected_effect=args.expected_effect or f"{args.action} {args.target}",
            capability=_cap_from_args(args),
            approval_token=getattr(args, "token", None))
        print(json.dumps(r, indent=2))
        return 0 if r.get("outcome") == "allow" else 1
    except BrokerUnavailable as e:
        print(f"broker unavailable (fail closed): {e}")
        return 1


def main():
    p = argparse.ArgumentParser(prog="veil-broker")
    p.add_argument("--run-dir", default=str(RUN_DIR))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start"); s.add_argument("--policy", default=str(POLICY)); s.set_defaults(fn=cmd_start)
    s = sub.add_parser("stop"); s.set_defaults(fn=cmd_stop)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)

    s = sub.add_parser("grant")
    s.add_argument("--capability", required=True)
    s.add_argument("--session", default="task-1")
    s.add_argument("--risk", default="medium")
    s.add_argument("--domains", nargs="*", default=None)
    s.add_argument("--paths", nargs="*", default=None)
    s.add_argument("--commands", nargs="*", default=None)
    s.set_defaults(fn=cmd_grant)

    s = sub.add_parser("propose")
    s.add_argument("--action", required=True)
    s.add_argument("--target", default="")
    s.add_argument("--expected-effect", default="")
    s.add_argument("--capability", required=True)
    s.add_argument("--session", default="task-1")
    s.add_argument("--risk", default="medium")
    s.add_argument("--domains", nargs="*", default=None)
    s.add_argument("--paths", nargs="*", default=None)
    s.add_argument("--commands", nargs="*", default=None)
    s.add_argument("--token", default=None)
    s.set_defaults(fn=cmd_propose)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
