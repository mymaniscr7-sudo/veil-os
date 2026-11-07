#!/usr/bin/env python3
"""
VEIL OS Remote API — the endpoint the Windows Remote Client talks to.

    Windows Remote Client
            |  HTTPS (or HTTP over an SSH tunnel / WireGuard in v1)
            v
    VEIL OS Remote API  (this file, stdlib only)
            |
            v
    VEIL Permission Broker  (approvals, policy — never bypassed)
            |
            v
    VEIL AI Runtime Container -> VEIL Video CLI/API

What the Windows client can do through it:
  - list / inspect video projects, timelines, transcripts, jobs
  - submit a task ("cut a 60s trailer from a-roll.mp4") for the AI
  - watch render / transcription jobs (status, progress, output, log)
  - approve or deny dangerous actions (publish, delete) — these go
    through the broker's admin socket, exactly like the Security
    Center. The Remote API never grants approval itself.
  - pause / resume / kill the AI task (OS signals, like the
    Security Center)

What it CANNOT do:
  - execute a video operation itself. It only records tasks and
    approvals. The AI runtime performs the work through VeilVideo,
    which proposes every step to the broker. A compromised remote
    client can submit tasks and approve pending items the user
    sees — it cannot make the broker allow something the user
    never approved, and it cannot read anything outside the
    video projects dir.
  - read host files, credentials, or other tasks' workspaces.

Auth: Bearer token. The token lives in the VEIL_REMOTE_TOKEN env
var or a token file (run/remote-token, 0600). No token -> the
server refuses to start unless --allow-no-auth is passed, which
is only for loopback development. Production deployments must
terminate TLS in front of this (reverse proxy) or reach it over
an SSH tunnel / WireGuard — v1 serves plain HTTP on 127.0.0.1.

Run:
  VEIL_REMOTE_TOKEN=$(cat run/remote-token) python3 remote_api.py --port 8766
  curl -H "Authorization: Bearer $VEIL_REMOTE_TOKEN" \
    http://127.0.0.1:8766/api/video/state
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import signal
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
ROOT = HERE.parent
RUN_DIR = ROOT / "run"
PROJECTS_ROOT = HERE / "projects"
TASKS_DIR = HERE / "tasks"
sys.path.insert(0, str(ROOT / "veil-core"))

from broker_client import AdminClient, BrokerUnavailable  # noqa: E402
from project import Project  # noqa: E402


def _read_json_file(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def _all_jobs() -> list[dict]:
    out = []
    if not PROJECTS_ROOT.exists():
        return out
    for proj_dir in sorted(PROJECTS_ROOT.iterdir()):
        if not proj_dir.is_dir():
            continue
        jobs = _read_json_file(proj_dir / "jobs.json", [])
        out.extend(jobs)
    return sorted(out, key=lambda j: j.get("created_at", ""), reverse=True)


def _tasks() -> list[dict]:
    if not TASKS_DIR.exists():
        return []
    out = []
    for f in sorted(TASKS_DIR.glob("*.json"), reverse=True):
        out.append(_read_json_file(f, {}))
    return [t for t in out if t]


def _task_state():
    task = _read_json_file(RUN_DIR / "task.json",
                           {"status": "idle", "name": "No task running"})
    pid_info = _read_json_file(RUN_DIR / "task-pid.json", {})
    task["pid"] = pid_info.get("pid")
    task["paused"] = pid_info.get("paused", False)
    return task


class RemoteAPI:
    def __init__(self, token: str | None):
        self.token = token

    def authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        if self.token is None:
            return True  # --allow-no-auth (loopback dev only)
        auth = handler.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:], self.token)

    # -- handlers return (status, payload) --------------------------------
    def state(self) -> tuple[int, dict]:
        try:
            admin = AdminClient(run_dir=RUN_DIR)
            status = admin.status()
            pending = admin.list_pending()["pending"]
            approvals = admin.list_approvals()["approvals"]
            broker = {"ok": True, "pid": status.get("pid"),
                      "approvals_held": status.get("approvals_held", 0),
                      "pending": status.get("pending", 0)}
        except BrokerUnavailable as e:
            broker = {"ok": False, "error": str(e)}
            pending, approvals = [], []
        return 200, {
            "broker": broker, "task": _task_state(),
            "pending_approvals": pending, "approvals": approvals,
            "projects": Project.list(PROJECTS_ROOT),
            "jobs": _all_jobs()[:20], "tasks": _tasks()[:20],
            "now": time.time(),
        }

    def list_projects(self):
        return 200, {"projects": Project.list(PROJECTS_ROOT)}

    def get_project(self, project_id: str):
        try:
            proj = Project.open(PROJECTS_ROOT, project_id)
        except (ValueError, FileNotFoundError) as e:
            return 404, {"error": str(e)}
        tl = proj.load_timeline()
        return 200, {**proj.manifest(), "timeline": tl.to_dict(),
                     "transcript": proj.read_sidecar("transcript.json"),
                     "scenes": proj.read_sidecar("scenes.json"),
                     "jobs": _read_json_file(proj.root / "jobs.json", [])}

    def list_jobs(self, project_id: str | None):
        jobs = _all_jobs()
        if project_id:
            jobs = [j for j in jobs if j["project_id"] == project_id]
        return 200, {"jobs": jobs}

    def get_job(self, job_id: str):
        job = next((j for j in _all_jobs() if j["job_id"] == job_id), None)
        if job is None:
            return 404, {"error": f"no job {job_id}"}
        return 200, job

    def submit_task(self, body: dict):
        instruction = (body.get("instruction") or "").strip()
        if not instruction:
            return 400, {"error": "instruction is required"}
        project_id = body.get("project_id")
        if project_id:
            try:
                Project.open(PROJECTS_ROOT, project_id)
            except (ValueError, FileNotFoundError) as e:
                return 404, {"error": str(e)}
        else:
            proj = Project.create(PROJECTS_ROOT, name=body.get("name", ""),
                                  task="remote")
            project_id = proj.project_id
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        task = {"task_id": task_id, "project_id": project_id,
                "instruction": instruction, "status": "queued",
                "created_at": time.time(),
                "created_via": "remote-api"}
        (TASKS_DIR / f"{task_id}.json").write_text(json.dumps(task, indent=2))
        return 201, task

    def approve(self, proposal_id: str):
        try:
            return 200, AdminClient(run_dir=RUN_DIR).approve_pending(proposal_id)
        except BrokerUnavailable as e:
            return 503, {"error": str(e)}

    def deny(self, proposal_id: str):
        try:
            return 200, AdminClient(run_dir=RUN_DIR).deny_pending(proposal_id)
        except BrokerUnavailable as e:
            return 503, {"error": str(e)}

    def task_signal(self, action: str):
        pid_info = _read_json_file(RUN_DIR / "task-pid.json", {})
        pid = pid_info.get("pid")
        if not pid:
            return 404, {"error": "no task process recorded"}
        sig = {"pause": signal.SIGSTOP, "resume": signal.SIGCONT,
               "kill": signal.SIGTERM}[action]
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return 404, {"error": "task process is gone"}
        if action == "kill":
            (RUN_DIR / "task-pid.json").write_text(json.dumps({"pid": None, "paused": False}))
            try:
                admin = AdminClient(run_dir=RUN_DIR)
                for p in admin.list_pending().get("pending", []):
                    admin.deny_pending(p["proposal_id"])
            except Exception:
                pass
            return 200, {"killed": True}
        paused = action == "pause"
        (RUN_DIR / "task-pid.json").write_text(json.dumps({"pid": pid, "paused": paused}))
        return 200, {"paused": paused}


def _load_token(args) -> str | None:
    if args.allow_no_auth:
        return None
    if args.token:
        return args.token
    env = os.environ.get("VEIL_REMOTE_TOKEN")
    if env:
        return env
    token_file = RUN_DIR / "remote-token"
    if token_file.exists():
        return token_file.read_text().strip()
    return None


class Handler(BaseHTTPRequestHandler):
    api: RemoteAPI = None  # set by main()

    def log_message(self, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
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

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _auth_or_401(self) -> bool:
        if self.path == "/api/health":
            return True
        if not self.api.authorized(self):
            self._send_json({"error": "unauthorized: Bearer token required"}, 401)
            return False
        return True

    def do_GET(self):
        if not self._auth_or_401():
            return
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path == "/api/health":
            self._send_json({"ok": True, "service": "veil-remote-api"})
            return
        if parsed.path == "/api/video/state":
            code, payload = self.api.state()
        elif parsed.path == "/api/video/projects":
            code, payload = self.api.list_projects()
        elif len(parts) == 4 and parts[:3] == ["api", "video", "projects"]:
            code, payload = self.api.get_project(parts[3])
        elif parsed.path == "/api/video/jobs":
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            code, payload = self.api.list_jobs((q.get("project") or [None])[0])
        elif len(parts) == 4 and parts[:3] == ["api", "video", "jobs"]:
            code, payload = self.api.get_job(parts[3])
        elif parsed.path == "/api/video/tasks":
            code, payload = 200, {"tasks": _tasks()}
        else:
            code, payload = 404, {"error": "not found"}
        self._send_json(payload, code)

    def do_POST(self):
        if not self._auth_or_401():
            return
        body = self._read_body()
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts == ["api", "video", "tasks"]:
            code, payload = self.api.submit_task(body)
        elif len(parts) == 5 and parts[:3] == ["api", "video", "approvals"] and parts[4] == "approve":
            code, payload = self.api.approve(parts[3])
        elif len(parts) == 5 and parts[:3] == ["api", "video", "approvals"] and parts[4] == "deny":
            code, payload = self.api.deny(parts[3])
        elif len(parts) == 5 and parts[:3] == ["api", "video", "tasks"] and parts[4] in ("pause", "resume", "kill"):
            code, payload = self.api.task_signal(parts[4])
        else:
            code, payload = 404, {"error": "not found"}
        self._send_json(payload, code)


def main():
    import argparse
    p = argparse.ArgumentParser(description="VEIL OS Remote API (for the Windows client)")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--token", default=None, help="Bearer token (or VEIL_REMOTE_TOKEN / run/remote-token)")
    p.add_argument("--allow-no-auth", action="store_true",
                   help="loopback dev only: no token required")
    p.add_argument("--gen-token", action="store_true",
                   help="generate a token, save to run/remote-token (0600), print it once")
    args = p.parse_args()
    if args.gen_token:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        f = RUN_DIR / "remote-token"
        f.write_text(token)
        os.chmod(f, 0o600)
        print(f"token saved to {f} (0600). Keep it secret — it is the Windows client's login.")
        print(f"VEIL_REMOTE_TOKEN={token}")
        return
    token = _load_token(args)
    if token is None and not args.allow_no_auth:
        print("refusing to start: no Bearer token configured.\n"
              "  python3 remote_api.py --gen-token   # once, then share it with the Windows client\n"
              "  VEIL_REMOTE_TOKEN=... python3 remote_api.py\n"
              "  python3 remote_api.py --allow-no-auth   # loopback dev only", file=sys.stderr)
        raise SystemExit(2)
    Handler.api = RemoteAPI(token)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"VEIL Remote API: http://{args.host}:{args.port}/  (auth: {'none — dev only' if token is None else 'Bearer token'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
