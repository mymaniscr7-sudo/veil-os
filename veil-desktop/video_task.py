#!/usr/bin/env python3
"""
Video demo task — the AI edits video with VEIL Video, not a GUI.

Steps (all broker-gated, service mode — the real path):
  1. Create a fresh project (video.edit, auto — reversible write).
  2. Import two clips (synthetic media generated with ffmpeg, or the
     demo media shipped in veil-video/demo-work/).
  3. Probe one clip, build a timeline (cut + fade + title).
  4. Render it with ffmpeg as a background job and wait for it.
  5. Done — reports the render (duration, size, audio).

Transcribe is intentionally NOT in this task: faster-whisper is not
installed in v1, and transcribe must fail clearly rather than fake a
transcript. Publish is not attempted either — it needs user
confirmation every time by design (see demo.py on the host).

State in run/task.json, SIGTERM caught. Mirrors research_task.py.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
RUN_DIR = ROOT / "run"
sys.path.insert(0, str(ROOT / "veil-core"))
sys.path.insert(0, str(ROOT / "veil-video"))

from video_tool import VeilVideo  # noqa: E402
import probe as probe_mod  # noqa: E402

TASK_FILE = RUN_DIR / "task.json"
PROJECTS = ROOT / "veil-video" / "projects"
DEMO_WORK = ROOT / "veil-video" / "demo-work"
SESSION = "video-demo-1"

_state = {"name": "Video task — cut and render with VEIL Video",
          "objective": "Create a project, import clips, build a timeline, render it with ffmpeg.",
          "status": "running", "step": "starting", "updated_at": time.time()}


def set_state(status=None, step=None):
    if status:
        _state["status"] = status
    if step:
        _state["step"] = step
    _state["updated_at"] = time.time()
    TASK_FILE.write_text(json.dumps(_state))


def _on_term(*_):
    set_state("killed", "killed by user in Security Center (SIGTERM)")
    sys.exit(0)


def _make_clip(path: Path, duration: float, freq: int):
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=640x360:rate=30",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(path)], check=True, timeout=120)


def _wait(video, project_id, job_id, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, job = video.job_status(project_id, job_id)
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.5)
    return {"status": "timeout"}


def main():
    signal.signal(signal.SIGTERM, _on_term)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return _run()
    except Exception as e:
        set_state("failed", f"task crashed: {type(e).__name__}: {e}")
        return 1


def _run():
    set_state("running", "starting — broker-gated video editor")
    video = VeilVideo(session=SESSION, projects_root=PROJECTS,
                      broker_run_dir=RUN_DIR, broker_mode="service")

    # 1. Media: reuse the shipped demo clips, or generate them.
    DEMO_WORK.mkdir(parents=True, exist_ok=True)
    a_roll, b_roll = DEMO_WORK / "a-roll.mp4", DEMO_WORK / "b-roll.mp4"
    if not a_roll.exists() or not b_roll.exists():
        set_state("running", "generating synthetic clips with ffmpeg")
        _make_clip(a_roll, 6.0, 440)
        _make_clip(b_roll, 4.0, 660)

    # 2. Create a fresh project (auto id — every run is independent).
    set_state("running", "creating project")
    d, proj = video.create_project(None, name="Video demo", width=640, height=360, fps=30.0)
    if not d.allowed or proj is None:
        set_state("failed", f"create project denied: {d.reason}")
        return 1
    pid = proj["project_id"]

    # 3. Import + probe.
    set_state("running", f"importing clips into {pid}")
    for src in (a_roll, b_roll):
        d, meta = video.import_media(pid, str(src))
        if not d.allowed or meta is None:
            set_state("failed", f"import {src.name} denied: {d.reason}")
            return 1
    d, meta = video.probe_media(pid, "a-roll.mp4")
    if not d.allowed or meta is None:
        set_state("failed", f"probe denied: {d.reason}")
        return 1

    # 4. Timeline: cut + fade + title.
    set_state("running", "building timeline (cut + fade + title)")
    for clip in (
        {"clip_id": "c1", "source": "a-roll.mp4", "track": "v1", "start": 0,
         "in_point": 1.0, "out_point": 4.0},
        {"clip_id": "c2", "source": "b-roll.mp4", "track": "v1", "start": 3.0,
         "in_point": 0.0, "out_point": 3.0, "transition_in": "fade"},
        {"clip_id": "t1", "source": "", "track": "text", "start": 0.5,
         "in_point": 0, "out_point": 2.5, "duration": 2.0, "text": "VEIL Video"},
    ):
        d, _ = video.add_clip(pid, clip)
        if not d.allowed:
            set_state("failed", f"add clip {clip['clip_id']} denied: {d.reason}")
            return 1

    # 5. Render (background job) and wait.
    set_state("running", "rendering with ffmpeg")
    d, job = video.render(pid, output_name="render.mp4")
    if not d.allowed or job is None:
        set_state("failed", f"render denied: {d.reason}")
        return 1
    job = _wait(video, pid, job["job_id"])
    if job["status"] != "done":
        set_state("failed", f"render {job['status']}: {job.get('error') or job}")
        return 1
    out = probe_mod.probe(Path(job["output"]))
    set_state("done",
              f"render done — {pid}: {out['duration_seconds']}s, "
              f"{out['video']['width']}x{out['video']['height']}, "
              f"audio: {out['has_audio']} (ffmpeg, broker-gated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
