#!/usr/bin/env python3
"""
veil-video CLI — the AI-first video editor, on the command line.

    veil-video --task demo-1 create --name "Launch cut"
    veil-video --task demo-1 import demo-1 /tmp/a-roll.mp4
    veil-video --task demo-1 probe demo-1 a-roll.mp4
    veil-video --task demo-1 add-clip demo-1 --clip '{"clip_id":"c1","source":"a-roll.mp4","track":"v1","start":0,"in_point":2,"out_point":12}'
    veil-video --task demo-1 timeline demo-1
    veil-video --task demo-1 transcribe demo-1 a-roll.mp4
    veil-video --task demo-1 render demo-1
    veil-video --task demo-1 jobs demo-1
    veil-video --task demo-1 job demo-1 job-xxxxxxxx
    veil-video --task demo-1 publish demo-1 render.mp4 /home/user/Videos/out.mp4

Every mutating command goes through the broker via VeilVideo
(service mode by default — the broker must be running; use
--broker inprocess with a policy file for offline tests only).
--json prints machine-readable output for the AI runtime.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from video_tool import VeilVideo  # noqa: E402


def _emit(obj, as_json: bool):
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(json.dumps(obj, indent=2, default=str) if isinstance(obj, dict) else obj)


def _result(decision, data, as_json: bool, wait_job: bool = False,
            video: VeilVideo | None = None, project_id: str | None = None):
    """Print a decision + data. For jobs, --wait polls until done."""
    payload = {"outcome": decision.outcome, "allowed": decision.allowed,
               "reason": decision.reason, "data": data}
    if wait_job and data and isinstance(data, dict) and data.get("job_id"):
        job_id = data["job_id"]
        while True:
            time.sleep(1)
            d2, job = video.job_status(project_id, job_id)
            if not d2.allowed:
                payload["data"] = {"job_id": job_id, "status": "unknown",
                                   "reason": d2.reason}
                break
            payload["data"] = job
            if job["status"] in ("done", "failed", "cancelled"):
                break
    _emit(payload, as_json)
    return 0 if decision.allowed else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="veil-video",
                                description="VEIL Video — AI-first video editor")
    p.add_argument("--task", default="default", help="VEIL task/session id")
    p.add_argument("--projects", default=str(HERE / "projects"),
                   help="projects root dir")
    p.add_argument("--broker", choices=["service", "inprocess"],
                   default="service")
    p.add_argument("--policy", default=None, help="policy file (inprocess only)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--wait", action="store_true",
                   help="wait for a started job to finish")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create"); c.add_argument("project_id", nargs="?")
    c.add_argument("--name", default=""); c.add_argument("--width", type=int, default=1920)
    c.add_argument("--height", type=int, default=1080); c.add_argument("--fps", type=float, default=30.0)
    sub.add_parser("list")
    s = sub.add_parser("summary"); s.add_argument("project_id")
    s = sub.add_parser("import"); s.add_argument("project_id"); s.add_argument("src")
    s.add_argument("--as", dest="dest_name", default=None)
    s = sub.add_parser("probe"); s.add_argument("project_id"); s.add_argument("filename")
    s = sub.add_parser("sheet"); s.add_argument("project_id"); s.add_argument("filename")
    s.add_argument("--thumbs", type=int, default=8)
    s = sub.add_parser("timeline"); s.add_argument("project_id")
    s = sub.add_parser("add-clip"); s.add_argument("project_id")
    s.add_argument("--clip", required=True, help="clip JSON")
    s = sub.add_parser("remove-clip"); s.add_argument("project_id"); s.add_argument("clip_id")
    s = sub.add_parser("move-clip"); s.add_argument("project_id"); s.add_argument("clip_id")
    s.add_argument("new_start", type=float)
    s = sub.add_parser("trim-clip"); s.add_argument("project_id"); s.add_argument("clip_id")
    s.add_argument("in_point", type=float); s.add_argument("out_point", type=float)
    s = sub.add_parser("transcribe"); s.add_argument("project_id"); s.add_argument("filename")
    s.add_argument("--model", default="base")
    s = sub.add_parser("transcript"); s.add_argument("project_id")
    s = sub.add_parser("find"); s.add_argument("project_id"); s.add_argument("query")
    s = sub.add_parser("scenes"); s.add_argument("project_id"); s.add_argument("filename")
    s.add_argument("--threshold", type=float, default=0.4)
    s = sub.add_parser("render"); s.add_argument("project_id")
    s.add_argument("--output", default="render.mp4")
    s.add_argument("--preset", default="veryfast"); s.add_argument("--crf", type=int, default=23)
    s = sub.add_parser("jobs"); s.add_argument("project_id")
    s = sub.add_parser("job"); s.add_argument("project_id"); s.add_argument("job_id")
    s = sub.add_parser("cancel-job"); s.add_argument("project_id"); s.add_argument("job_id")
    s = sub.add_parser("export-mlt"); s.add_argument("project_id")
    s.add_argument("--output", default="timeline.mlt")
    s = sub.add_parser("publish"); s.add_argument("project_id")
    s.add_argument("output_name"); s.add_argument("dest")
    s = sub.add_parser("delete-media"); s.add_argument("project_id"); s.add_argument("filename")

    args = p.parse_args(argv)
    video = VeilVideo(session=args.task, projects_root=Path(args.projects),
                      broker_mode=args.broker,
                      policy_path=Path(args.policy) if args.policy else None)
    cmd = args.cmd
    if cmd == "create":
        d, data = video.create_project(args.project_id, name=args.name,
                                       width=args.width, height=args.height, fps=args.fps)
    elif cmd == "list":
        d, data = video.list_projects()
    elif cmd == "summary":
        d, data = video.project_summary(args.project_id)
    elif cmd == "import":
        d, data = video.import_media(args.project_id, args.src, args.dest_name)
    elif cmd == "probe":
        d, data = video.probe_media(args.project_id, args.filename)
    elif cmd == "sheet":
        d, data = video.contact_sheet(args.project_id, args.filename, args.thumbs)
    elif cmd == "timeline":
        d, data = video.get_timeline(args.project_id)
    elif cmd == "add-clip":
        d, data = video.add_clip(args.project_id, json.loads(args.clip))
    elif cmd == "remove-clip":
        d, data = video.remove_clip(args.project_id, args.clip_id)
    elif cmd == "move-clip":
        d, data = video.move_clip(args.project_id, args.clip_id, args.new_start)
    elif cmd == "trim-clip":
        d, data = video.trim_clip(args.project_id, args.clip_id,
                                  args.in_point, args.out_point)
    elif cmd == "transcribe":
        d, data = video.transcribe(args.project_id, args.filename, model=args.model)
        return _result(d, data, args.json, wait_job=args.wait,
                       video=video, project_id=args.project_id)
    elif cmd == "transcript":
        d, data = video.transcript(args.project_id)
    elif cmd == "find":
        d, data = video.find_in_transcript(args.project_id, args.query)
    elif cmd == "scenes":
        d, data = video.detect_scenes(args.project_id, args.filename,
                                      threshold=args.threshold)
        return _result(d, data, args.json, wait_job=args.wait,
                       video=video, project_id=args.project_id)
    elif cmd == "render":
        d, data = video.render(args.project_id, output_name=args.output,
                               preset=args.preset, crf=args.crf)
        return _result(d, data, args.json, wait_job=args.wait,
                       video=video, project_id=args.project_id)
    elif cmd == "jobs":
        d, data = video.list_jobs(args.project_id)
    elif cmd == "job":
        d, data = video.job_status(args.project_id, args.job_id)
    elif cmd == "cancel-job":
        d, data = video.cancel_job(args.project_id, args.job_id)
    elif cmd == "export-mlt":
        d, data = video.export_mlt(args.project_id, args.output)
    elif cmd == "publish":
        d, data = video.publish(args.project_id, args.output_name, args.dest)
    elif cmd == "delete-media":
        d, data = video.delete_media(args.project_id, args.filename)
    else:  # pragma: no cover
        p.error(f"unknown command {cmd}")
    return _result(d, data, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
