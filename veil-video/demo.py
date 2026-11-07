#!/usr/bin/env python3
"""VEIL Video demo — end to end, no GUI, no fake data.

Generates two synthetic clips with ffmpeg, creates a project,
imports them, probes, builds a timeline (cut + fade + title),
detects scenes, renders, and shows that publish is blocked until
the user confirms. Run:

    python3 demo.py [--projects DIR]

Uses the in-process broker with the video-editor policy, so it
runs without the broker service. The real path is the service
(broker_service.py) + `veil-video` CLI; this demo proves the
timeline -> ffmpeg path and the broker gating.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from video_tool import VeilVideo  # noqa: E402
import probe as probe_mod  # noqa: E402


def make_clip(path: Path, duration: float, color: str, freq: int):
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=640x360:rate=30",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(path)], check=True, timeout=120)


def wait(video, project_id, job_id, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d, job = video.job_status(project_id, job_id)
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.5)
    return {"status": "timeout"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default=str(HERE / "projects"))
    args = ap.parse_args()
    projects = Path(args.projects)
    work = HERE / "demo-work"
    work.mkdir(exist_ok=True)
    a_roll, b_roll = work / "a-roll.mp4", work / "b-roll.mp4"
    print("generating synthetic media...")
    make_clip(a_roll, 6.0, "bars", 440)
    make_clip(b_roll, 4.0, "bars", 660)

    video = VeilVideo(session="demo", projects_root=projects,
                      broker_mode="inprocess",
                      policy_path=HERE / "policy-video-editor.json")
    d, proj = video.create_project("demo-cut", name="Demo cut",
                                   width=640, height=360, fps=30.0)
    print(f"project: {proj['project_id']} ({d.outcome})")
    for src in (a_roll, b_roll):
        d, meta = video.import_media("demo-cut", str(src))
        print(f"import {src.name}: {d.outcome} — {meta['duration_seconds']}s, "
              f"{meta['video']['width']}x{meta['video']['height']}")
    d, _ = video.add_clip("demo-cut", {"clip_id": "c1", "source": "a-roll.mp4",
                                       "track": "v1", "start": 0,
                                       "in_point": 1.0, "out_point": 4.0})
    print(f"add c1: {d.outcome}")
    d, _ = video.add_clip("demo-cut", {"clip_id": "c2", "source": "b-roll.mp4",
                                       "track": "v1", "start": 3.0,
                                       "in_point": 0.0, "out_point": 3.0,
                                       "transition_in": "fade"})
    print(f"add c2 (fade in): {d.outcome}")
    d, _ = video.add_clip("demo-cut", {"clip_id": "t1", "source": "",
                                       "track": "text", "start": 0.5,
                                       "in_point": 0, "out_point": 2.5,
                                       "duration": 2.0, "text": "VEIL Video"})
    print(f"add title: {d.outcome}")
    d, job = video.detect_scenes("demo-cut", "a-roll.mp4")
    job = wait(video, "demo-cut", job["job_id"])
    print(f"scenes: {job['status']} — {job.get('output')}")
    d, job = video.render("demo-cut", output_name="render.mp4")
    print(f"render started: {job['job_id']} ({d.outcome})")
    job = wait(video, "demo-cut", job["job_id"])
    print(f"render: {job['status']} — {job.get('output') or job.get('error')}")
    if job["status"] == "done":
        meta = probe_mod.probe(Path(job["output"]))
        print(f"output: {meta['duration_seconds']}s, "
              f"{meta['video']['width']}x{meta['video']['height']}, "
              f"audio: {meta['has_audio']}")
    d, _ = video.publish("demo-cut", "render.mp4", str(work / "published.mp4"))
    print(f"publish without approval: {d.outcome} — {d.reason}")
    print(f"published file exists? {(work / 'published.mp4').exists()} (must be False)")
    print("\nDone. Project at:", projects / "demo-cut")
    print("Try the CLI: python3 cli.py --task demo --broker inprocess "
          "--policy policy-video-editor.json --json timeline demo-cut")


if __name__ == "__main__":
    main()
