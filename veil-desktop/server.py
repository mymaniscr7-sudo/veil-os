#!/usr/bin/env python3
"""
VEIL Security Center — local server (Stage 3: VEIL Desktop, v1)

A view + control client for policy state. It is NOT the policy
itself: every toggle, approval, and rollback goes through the
broker service (admin socket) or the sandbox — the UI never edits
policy files directly, and it never grants itself approval.

What it shows (research v2):
  - current task + objective, status, current step
  - active capabilities + expiration times, files/domains in scope
  - pending approvals (exact action, target, expected effect — not
    just "Allow AI to continue?")
  - recent actions + results (journal)
  - snapshot + rollback controls
  - Pause AI / Kill Task — OS signals (SIGSTOP/SIGTERM) to the task
    process, not a request to the model. Works even if the model
    is misbehaving.

Run:
  python3 server.py [--port 8765]
  open http://127.0.0.1:8765/
Binds 127.0.0.1 only. Stdlib only, no dependencies.
If the broker isn't running, the server starts it.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
ROOT = HERE.parent
RUN_DIR = ROOT / "run"
sys.path.insert(0, str(ROOT / "veil-core"))
sys.path.insert(0, str(ROOT / "veil-sandbox"))

from broker_client import AdminClient, BrokerClient, BrokerUnavailable  # noqa: E402
from limits import probe_cgroups  # noqa: E402

TASK_FILE = RUN_DIR / "task.json"
TASK_PID_FILE = RUN_DIR / "task-pid.json"
JOURNAL = ROOT / "veil-core" / "audit-v2.log.jsonl"
SNAPSHOT_DIR = ROOT / "veil-sandbox" / ".snapshots"


def _read_json_file(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _journal_tail(n=30):
    try:
        lines = Path(JOURNAL).read_text().splitlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return list(reversed(out))
    except OSError:
        return []


def _snapshots():
    out = []
    if not SNAPSHOT_DIR.exists():
        return out
    for d in sorted(SNAPSHOT_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta = _read_json_file(d / ".veil-meta.json", {})
        files = [p.name for p in d.iterdir() if p.name != ".veil-meta.json"]
        out.append({
            "snapshot_id": d.name,
            "workspace": meta.get("workspace", "unknown"),
            "created_at": meta.get("created_at", ""),
            "files": files[:10],
            "file_count": len(files),
        })
    return out


def _task_state():
    task = _read_json_file(TASK_FILE, {"status": "idle", "name": "No task running",
                                       "objective": "Start a demo task to see it here.",
                                       "step": "-", "updated_at": 0})
    pid_info = _read_json_file(TASK_PID_FILE, {})
    task["pid"] = pid_info.get("pid")
    task["paused"] = pid_info.get("paused", False)
    if task.get("pid"):
        try:
            os.kill(task["pid"], 0)
            # A zombie (finished but unreaped) is not alive.
            try:
                with open(f"/proc/{task['pid']}/stat") as f:
                    alive = f.read().rsplit(")", 1)[1].split()[0] != "Z"
            except (OSError, IndexError):
                alive = True
            task["alive"] = alive
        except ProcessLookupError:
            task["alive"] = False
        if not task["alive"] and task["status"] in ("running", "waiting"):
            task["status"] = "finished"
    else:
        task["alive"] = False
    return task


def _browser_sessions():
    """Live browser sessions (veil-browser-cli): one per VEIL task.
    Runs the CLI's own `status` per session — read-only, no navigation."""
    try:
        from browser_tool import find_cli_binary  # noqa: E402
    except Exception:
        return []
    cli = find_cli_binary()
    if cli is None:
        return []
    sessions_dir = Path.home() / ".cache" / "veil-browser" / "sessions"
    if not sessions_dir.exists():
        return []
    out = []
    for d in sorted(sessions_dir.iterdir()):
        if not d.is_dir() or not d.name.startswith("veil-"):
            continue
        # Only query live daemons: running `status` on a stopped
        # session would auto-start (resurrect) its daemon.
        if not (d / "daemon.sock").exists():
            continue
        try:
            r = subprocess.run([str(cli), "--session", d.name, "--json", "status"],
                               capture_output=True, text=True, timeout=5)
            info = json.loads(r.stdout) if r.stdout.strip() else {}
        except (subprocess.TimeoutExpired, ValueError, OSError):
            info = {}
        data = info.get("data", {}) if isinstance(info, dict) else {}
        out.append({
            "session": d.name,
            "ok": bool(info.get("ok")),
            "tabs": data.get("tabs"),
            "active_url": data.get("active_url"),
            "active_title": data.get("active_title"),
            "summary": info.get("summary", ""),
        })
    return out


def _video_status():
    """VEIL Video projects + jobs + queued remote tasks, read-only.
    Reads project dirs and jobs.json files only — it never starts a
    render, a broker call, or a browser/ffmpeg process. If the
    veil-video package is absent, returns an empty summary."""
    video_dir = ROOT / "veil-video"
    if not video_dir.exists():
        return {"available": False, "projects": [], "jobs": [], "tasks": []}
    projects_root = video_dir / "projects"
    projects, jobs = [], []
    if projects_root.exists():
        for d in sorted(projects_root.iterdir()):
            if not d.is_dir():
                continue
            manifest = _read_json_file(d / "project.json", None)
            if manifest is None:
                continue
            tl = _read_json_file(d / "timeline.json", {})
            renders = sorted((d / "renders").glob("*.mp4")) if (d / "renders").exists() else []
            projects.append({
                "project_id": manifest.get("project_id", d.name),
                "name": manifest.get("name", ""),
                "status": manifest.get("status", ""),
                "clip_count": len(tl.get("clips", [])),
                "render_count": len(renders),
                "latest_render": renders[-1].name if renders else None,
                "has_transcript": (d / "transcript.json").exists(),
            })
            for j in _read_json_file(d / "jobs.json", []):
                jobs.append(j)
    tasks = []
    tasks_dir = video_dir / "tasks"
    if tasks_dir.exists():
        for f in sorted(tasks_dir.glob("*.json"), reverse=True)[:10]:
            tasks.append(_read_json_file(f, {}))
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return {"available": True, "projects": projects,
            "jobs": jobs[:20], "tasks": [t for t in tasks if t]}


def get_state():
    try:
        admin = AdminClient(run_dir=RUN_DIR)
        status = admin.status()
        policy = admin.get_policy()["policy"]
        pending = admin.list_pending()["pending"]
        approvals = admin.list_approvals()["approvals"]
        broker = {"ok": True, "pid": status.get("pid"),
                  "approvals_held": status.get("approvals_held", 0),
                  "pending": status.get("pending", 0)}
    except BrokerUnavailable as e:
        broker = {"ok": False, "error": str(e)}
        policy, pending, approvals = {}, [], []
    import shutil
    bwrap_ok = shutil.which("bwrap") is not None
    try:
        from browser_tool import find_cli_binary  # noqa: E402
        cli = find_cli_binary()
    except Exception:
        cli = None
    return {
        "broker": broker,
        "bwrap_available": bwrap_ok,
        "browser_cli": {"available": cli is not None, "binary": str(cli) if cli else None},
        "browser_sessions": _browser_sessions(),
        "video": _video_status(),
        "policy": policy,
        "network_state": (policy.get("network", {}) or {}).get("state", "unknown"),
        "allowed_domains": (policy.get("network", {}) or {}).get("allowed_domains", []),
        "capabilities": policy.get("capabilities", {}),
        "pending": pending,
        "approvals": approvals,
        "journal": _journal_tail(30),
        "snapshots": _snapshots(),
        "task": _task_state(),
        "limits": {
            "cpu_seconds": 30, "memory_mb": 512, "max_processes": 512,
            "max_file_mb": 100, "timeout_seconds": 30,
            "cgroup_probe": probe_cgroups(),
        },
        "now": time.time(),
    }


def _ensure_broker():
    try:
        AdminClient(run_dir=RUN_DIR).status()
        return True
    except BrokerUnavailable:
        pass
    subprocess.run([sys.executable, str(ROOT / "veil-core" / "veil-broker.py"), "start"],
                   check=False, capture_output=True)
    try:
        AdminClient(run_dir=RUN_DIR).status()
        return True
    except BrokerUnavailable:
        return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except ValueError:
            return {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if self.path == "/api/state":
            self._send_json(get_state())
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        body = self._read_body()
        try:
            admin = AdminClient(run_dir=RUN_DIR)
        except Exception as e:
            self._send_json({"error": str(e)}, 503)
            return
        try:
            if self.path == "/api/approve":
                self._send_json(admin.approve_pending(body["proposal_id"]))
            elif self.path == "/api/deny":
                self._send_json(admin.deny_pending(body["proposal_id"]))
            elif self.path == "/api/network":
                self._send_json(admin.set_network_state(body["state"]))
            elif self.path == "/api/capability":
                self._send_json(admin.set_capability(body["capability"], bool(body["allowed"])))
            elif self.path == "/api/snapshot/rollback":
                snap_id = body["snapshot_id"]
                snap_dir = SNAPSHOT_DIR / snap_id
                meta = _read_json_file(snap_dir / ".veil-meta.json", {})
                ws = Path(meta.get("workspace") or (ROOT / "veil-sandbox" / "test-workspace"))
                from sandbox import VeilSandbox  # noqa: E402
                VeilSandbox(ws, broker_mode="inprocess").rollback(snap_id)
                self._send_json({"rolled_back": snap_id, "workspace": str(ws)})
            elif self.path == "/api/task/start":
                self._start_task(body.get("task", "demo"))
                self._send_json({"started": True, "task": body.get("task", "demo")})
            elif self.path == "/api/task/pause":
                self._signal_task(signal.SIGSTOP, paused=True)
                self._send_json({"paused": True})
            elif self.path == "/api/task/resume":
                self._signal_task(signal.SIGCONT, paused=False)
                self._send_json({"paused": False})
            elif self.path == "/api/task/kill":
                self._signal_task(signal.SIGTERM, paused=False, clear=True)
                self._send_json({"killed": True})
            else:
                self._send_json({"error": "not found"}, 404)
        except BrokerUnavailable as e:
            self._send_json({"error": f"broker unavailable: {e}"}, 503)
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 400)

    def _start_task(self, task="demo"):
        # Don't start a second task while one is alive. A finished
        # task leaves its pid behind (the server never wait()s on the
        # child, so it lingers as a zombie and os.kill(pid, 0) still
        # succeeds). Treat the pid as stale when task.json already
        # reports a terminal state — otherwise no second task can
        # ever start after the first one finishes.
        pid_info = _read_json_file(TASK_PID_FILE, {})
        if pid_info.get("pid"):
            task_state = _read_json_file(TASK_FILE, {}).get("status")
            if task_state not in ("done", "failed", "denied", "killed"):
                try:
                    os.kill(pid_info["pid"], 0)
                    return
                except ProcessLookupError:
                    pass
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        scripts = {"researcher": "research_task.py", "video": "video_task.py"}
        script = scripts.get(task, "demo_task.py")
        proc = subprocess.Popen(
            [sys.executable, str(HERE / script)],
            stdout=open(RUN_DIR / "task.log", "ab"),
            stderr=subprocess.STDOUT, start_new_session=True)
        TASK_PID_FILE.write_text(json.dumps({"pid": proc.pid, "paused": False}))

    def _signal_task(self, sig, paused=False, clear=False):
        pid_info = _read_json_file(TASK_PID_FILE, {})
        pid = pid_info.get("pid")
        if not pid:
            return
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        if clear:
            # Killing a task also clears its pending approvals: a dead
            # task must not get approved after the fact.
            try:
                admin = AdminClient(run_dir=RUN_DIR)
                for p in admin.list_pending().get("pending", []):
                    admin.deny_pending(p["proposal_id"])
            except Exception:
                pass
            TASK_PID_FILE.write_text(json.dumps({"pid": None, "paused": False}))
            TASK_FILE.write_text(json.dumps({
                "status": "killed", "name": "Demo task",
                "objective": "Killed by user in Security Center.",
                "step": "killed", "updated_at": time.time()}))
        else:
            TASK_PID_FILE.write_text(json.dumps({"pid": pid, "paused": paused}))


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    # Default stays loopback (the Security Center has no auth by design).
    # The Docker image passes --host 0.0.0.0 and publishes the port on
    # 127.0.0.1 only — see docker/README.md.
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    _ensure_broker()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"VEIL Security Center: http://{args.host}:{args.port}/  (broker: {'up' if _ensure_broker() else 'DOWN — start it with veil-broker.py start'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
