#!/usr/bin/env python3
"""
VEIL Remote Client — example client for the Windows computer.

This is the reference client the Windows app is built from: plain
Python 3, stdlib only (urllib), so it runs on Windows without
installing anything beyond Python. It exercises every Remote API
endpoint a real Windows client needs:

  - dashboard state (projects, jobs, pending approvals, task)
  - submit a task for the AI ("cut a 60s trailer...")
  - watch a render/transcription job until it finishes
  - approve / deny a pending dangerous action
  - pause / resume / kill the AI task

Usage (on the Windows machine, with the Remote API reachable —
via SSH tunnel, WireGuard, or a TLS reverse proxy in production):

  set VEIL_REMOTE_URL=http://127.0.0.1:8766
  set VEIL_REMOTE_TOKEN=<token from `python3 remote_api.py --gen-token`>
  python remote_client.py state
  python remote_client.py submit --instruction "Cut a 60s trailer from a-roll.mp4, keep the funniest lines"
  python remote_client.py watch job-xxxxxxxx
  python remote_client.py approve prop-xxxxxxxx
  python remote_client.py pause

A native Windows client (WinUI / Electron / Tauri) would call the
same endpoints; the API contract is in remote_api.py and
WINDOWS-CLIENT.md. Nothing here talks to the broker directly —
approvals go through the Remote API, which goes through the
broker's admin socket.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("VEIL_REMOTE_URL", "http://127.0.0.1:8766").rstrip("/")
TOKEN = os.environ.get("VEIL_REMOTE_TOKEN", "")


def _req(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TOKEN}"} if TOKEN else
                {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode() or "{}")
        except ValueError:
            detail = {"error": f"HTTP {e.code}"}
        print(f"error: HTTP {e.code}: {detail}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as e:
        print(f"error: cannot reach {BASE}: {e.reason}", file=sys.stderr)
        print("Is the Remote API running? Is the tunnel/VPN up?", file=sys.stderr)
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser(prog="remote_client.py",
                                description="VEIL Remote Client (reference, Windows-ready)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state", help="dashboard: projects, jobs, pending approvals, task")
    sub.add_parser("projects", help="list video projects")
    s = sub.add_parser("project"); s.add_argument("project_id")
    sub.add_parser("jobs", help="list jobs (all projects)")
    s = sub.add_parser("job"); s.add_argument("job_id")
    s = sub.add_parser("watch"); s.add_argument("job_id")
    s.add_argument("--interval", type=float, default=2.0)
    s = sub.add_parser("submit"); s.add_argument("--instruction", required=True)
    s.add_argument("--project", default=None); s.add_argument("--name", default="")
    sub.add_parser("tasks", help="list submitted tasks")
    s = sub.add_parser("approve"); s.add_argument("proposal_id")
    s = sub.add_parser("deny"); s.add_argument("proposal_id")
    sub.add_parser("pause"); sub.add_parser("resume"); sub.add_parser("kill")
    args = p.parse_args()

    if args.cmd == "state":
        data = _req("GET", "/api/video/state")
        print(f"broker: {'up' if data['broker'].get('ok') else 'DOWN ' + str(data['broker'].get('error'))}")
        print(f"task: {data['task'].get('status')} — {data['task'].get('name')}")
        print(f"pending approvals: {len(data['pending_approvals'])}")
        for a in data["pending_approvals"]:
            print(f"  {a['proposal_id']}: {a['action']} {a['target']} — {a['expected_effect']}")
        print(f"projects: {len(data['projects'])}")
        for pr in data["projects"]:
            print(f"  {pr['project_id']}: {pr.get('name')} — {pr.get('clip_count')} clips, "
                  f"{pr.get('duration_seconds')}s, {pr.get('render_count')} renders")
        print(f"jobs: {len(data['jobs'])}")
        for j in data["jobs"]:
            print(f"  {j['job_id']}: {j['type']} {j['project_id']} — {j['status']} "
                  f"{int(j.get('progress') or 0) * 100}%")
    elif args.cmd == "projects":
        for pr in _req("GET", "/api/video/projects")["projects"]:
            print(f"{pr['project_id']}: {pr.get('name')} — {pr.get('clip_count')} clips, {pr.get('duration_seconds')}s")
    elif args.cmd == "project":
        print(json.dumps(_req("GET", f"/api/video/projects/{args.project_id}"), indent=2))
    elif args.cmd == "jobs":
        for j in _req("GET", "/api/video/jobs")["jobs"]:
            print(f"{j['job_id']}: {j['type']} {j['project_id']} — {j['status']} {j.get('progress')}")
    elif args.cmd == "job":
        print(json.dumps(_req("GET", f"/api/video/jobs/{args.job_id}"), indent=2))
    elif args.cmd == "watch":
        while True:
            job = _req("GET", f"/api/video/jobs/{args.job_id}")
            pct = int((job.get("progress") or 0) * 100)
            print(f"{job['job_id']}: {job['status']} {pct}% — {job.get('output') or job.get('error') or ''}")
            if job["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(args.interval)
    elif args.cmd == "submit":
        body = {"instruction": args.instruction}
        if args.project:
            body["project_id"] = args.project
        if args.name:
            body["name"] = args.name
        print(json.dumps(_req("POST", "/api/video/tasks", body), indent=2))
    elif args.cmd == "tasks":
        for t in _req("GET", "/api/video/tasks")["tasks"]:
            print(f"{t['task_id']}: {t['project_id']} — {t['status']} — {t['instruction'][:80]}")
    elif args.cmd == "approve":
        print(json.dumps(_req("POST", f"/api/video/approvals/{args.proposal_id}/approve", {}), indent=2))
    elif args.cmd == "deny":
        print(json.dumps(_req("POST", f"/api/video/approvals/{args.proposal_id}/deny", {}), indent=2))
    elif args.cmd in ("pause", "resume", "kill"):
        print(json.dumps(_req("POST", f"/api/video/tasks/x/{args.cmd}", {}), indent=2))


if __name__ == "__main__":
    main()
