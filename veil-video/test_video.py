#!/usr/bin/env python3
"""VEIL Video tests — real ffmpeg, real broker (in-process), real files.

No mocks of the thing under test: media is generated with ffmpeg
(testsrc + sine tone), the timeline is validated and rendered with
the real renderer, and every VeilVideo call goes through a real
in-process BrokerV2 with the video-editor policy. The only thing
faked is the transcript backend: faster-whisper is not installed
on the dev host, so transcribe tests assert the honest failure
(TranscribeUnavailable with the install hint) instead of a fake
transcript.

Run:  python3 test_video.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "veil-core"))

from project import Clip, Project, Timeline  # noqa: E402
import probe as probe_mod  # noqa: E402
import render as render_mod  # noqa: E402
import transcribe as transcribe_mod  # noqa: E402
from video_tool import VeilVideo  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def make_test_media(path: Path, duration: float = 6.0):
    """Synthetic clip: colour bars + 440Hz tone, with audio."""
    r = subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=640x360:rate=30",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(path)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"ffmpeg test media failed: {r.stderr[:300]}"
    return path


def wait_job(video: VeilVideo, project_id: str, job_id: str, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d, job = video.job_status(project_id, job_id)
        assert d.allowed, f"job_status denied: {d.reason}"
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.5)
    raise TimeoutError(f"job {job_id} did not finish in {timeout}s")


def main():
    print("== VEIL Video tests ==")
    tmp = Path(tempfile.mkdtemp(prefix="veil-video-test-"))
    projects = tmp / "projects"
    policy = HERE / "policy-video-editor.json"
    video = VeilVideo(session="test-1", projects_root=projects,
                      broker_mode="inprocess", policy_path=policy)

    # 1. project model ------------------------------------------------
    print("-- project model --")
    proj = Project.create(projects, "demo-cut", name="Demo cut",
                          width=640, height=360, fps=30.0)
    check("create project", proj.project_id == "demo-cut")
    try:
        Project.create(projects, "BAD ID!")
        check("bad project id rejected", False)
    except ValueError:
        check("bad project id rejected", True)
    try:
        Project.open(projects, "../escape")
        check("path traversal rejected", False)
    except (ValueError, FileNotFoundError):
        check("path traversal rejected", True)

    # 2. probe (real ffprobe) ----------------------------------------
    print("-- probe --")
    src = make_test_media(tmp / "a-roll.mp4", duration=6.0)
    src2 = make_test_media(tmp / "b-roll.mp4", duration=4.0)
    meta = probe_mod.probe(src)
    check("probe duration ~6s", meta["duration_seconds"] is not None
          and abs(meta["duration_seconds"] - 6.0) < 0.3, str(meta))
    check("probe video 640x360", meta["video"]["width"] == 640
          and meta["video"]["height"] == 360, str(meta))
    check("probe has audio", meta["has_audio"])

    # 3. import + probe through the broker-gated tool -----------------
    print("-- VeilVideo import/probe --")
    d, data = video.import_media("demo-cut", str(src))
    check("import allowed", d.allowed, d.reason)
    check("import probed", data and abs(data["duration_seconds"] - 6.0) < 0.3)
    d, data = video.import_media("demo-cut", str(src2), dest_name="b-roll.mp4")
    check("import second clip", d.allowed, d.reason)
    d, data = video.probe_media("demo-cut", "a-roll.mp4")
    check("probe via tool", d.allowed and data["has_video"])

    # 4. timeline edits + validation ----------------------------------
    print("-- timeline --")
    clip_a = {"clip_id": "c1", "source": "a-roll.mp4", "track": "v1",
              "start": 0, "in_point": 1.0, "out_point": 4.0}
    d, tl = video.add_clip("demo-cut", clip_a)
    check("add clip c1", d.allowed, d.reason)
    clip_b = {"clip_id": "c2", "source": "b-roll.mp4", "track": "v1",
              "start": 3.0, "in_point": 0.0, "out_point": 3.0,
              "transition_in": "fade"}
    d, tl = video.add_clip("demo-cut", clip_b)
    check("add clip c2 with fade", d.allowed, d.reason)
    check("timeline duration 6s", tl and abs(
        max(c["start"] + (c["out_point"] - c["in_point"]) for c in tl["clips"]) - 6.0) < 0.01)
    # Invalid: out_point beyond source duration.
    bad = {"clip_id": "c3", "source": "a-roll.mp4", "track": "v1",
           "start": 6.0, "in_point": 0.0, "out_point": 99.0}
    d, _ = video.add_clip("demo-cut", bad)
    check("out-of-range clip denied", not d.allowed, d.reason)
    # Invalid: v1 overlap.
    overlap = {"clip_id": "c4", "source": "a-roll.mp4", "track": "v1",
               "start": 2.0, "in_point": 0.0, "out_point": 1.0}
    d, _ = video.add_clip("demo-cut", overlap)
    check("v1 overlap denied", not d.allowed, d.reason)
    # Move + trim.
    d, tl = video.trim_clip("demo-cut", "c1", 0.5, 3.5)
    check("trim clip", d.allowed, d.reason)
    d, tl = video.move_clip("demo-cut", "c2", 3.0)
    check("move clip", d.allowed, d.reason)
    d, tl = video.get_timeline("demo-cut")
    check("timeline reads back", d.allowed and len(tl["clips"]) == 2)
    # Text overlay clip.
    d, tl = video.add_clip("demo-cut", {
        "clip_id": "t1", "source": "", "track": "text", "start": 0.5,
        "in_point": 0, "out_point": 2.5, "duration": 2.0,
        "text": "VEIL Video test"})
    check("add text clip", d.allowed, d.reason)

    # 5. scenes (real ffmpeg) -----------------------------------------
    print("-- scenes --")
    d, job = video.detect_scenes("demo-cut", "a-roll.mp4")
    check("scenes job started", d.allowed and job["type"] == "scenes", d.reason)
    job = wait_job(video, "demo-cut", job["job_id"])
    check("scenes job done", job["status"] == "done", str(job.get("error")))
    scenes = proj.read_sidecar("scenes.json")
    check("scenes.json written", scenes is not None and scenes["scene_count"] >= 1)

    # 6. contact sheet (real ffmpeg) ------------------------------------
    print("-- contact sheet --")
    d, data = video.contact_sheet("demo-cut", "a-roll.mp4", thumbs=4)
    check("contact sheet", d.allowed and Path(data["contact_sheet"]).exists(), d.reason)

    # 7. transcribe: honest failure without a backend -------------------
    print("-- transcribe --")
    avail = transcribe_mod.backend_available()
    print(f"    backends on this host: {avail}")
    if not avail["faster_whisper"] and not avail["whisper_cli"]:
        try:
            transcribe_mod.transcribe(src)
            check("transcribe fails clearly without backend", False)
        except transcribe_mod.TranscribeUnavailable as e:
            check("transcribe fails clearly without backend", "faster-whisper" in str(e))
        d, job = video.transcribe("demo-cut", "a-roll.mp4")
        check("transcribe job started", d.allowed, d.reason)
        job = wait_job(video, "demo-cut", job["job_id"])
        check("transcribe job failed honestly (no backend)",
              job["status"] == "failed" and "faster-whisper" in (job.get("error") or ""),
              str(job))
    else:
        d, job = video.transcribe("demo-cut", "a-roll.mp4")
        job = wait_job(video, "demo-cut", job["job_id"], timeout=600)
        check("transcribe job done", job["status"] == "done", str(job.get("error")))
        tr = proj.read_sidecar("transcript.json")
        check("transcript.json written", tr is not None)

    # 8. render (real ffmpeg) -------------------------------------------
    print("-- render --")
    d, job = video.render("demo-cut", output_name="render.mp4")
    check("render job started", d.allowed and job["type"] == "render", d.reason)
    job = wait_job(video, "demo-cut", job["job_id"], timeout=300)
    check("render job done", job["status"] == "done", str(job))
    out = projects / "demo-cut" / "renders" / "render.mp4"
    check("render output exists", out.exists())
    if out.exists():
        meta = probe_mod.probe(out)
        check("render duration ~6s", meta["duration_seconds"] is not None
              and abs(meta["duration_seconds"] - 6.0) < 0.5, str(meta))
        check("render 640x360", meta["video"]["width"] == 640
              and meta["video"]["height"] == 360, str(meta))
        check("render has audio (v1 clips carry their sound)",
              meta["has_audio"], str(meta))

    # 9. publish needs confirmation; delete needs step-up -----------------
    print("-- broker gating: publish / delete --")
    d, _ = video.publish("demo-cut", "render.mp4", str(tmp / "out.mp4"))
    check("publish needs confirmation", not d.allowed
          and d.outcome == "needs_user_confirmation", f"{d.outcome}: {d.reason}")
    check("publish did NOT copy", not (tmp / "out.mp4").exists())
    d, _ = video.delete_media("demo-cut", "b-roll.mp4")
    check("delete needs step-up", not d.allowed
          and d.outcome == "needs_step_up_authentication", f"{d.outcome}: {d.reason}")
    check("delete did NOT remove", (projects / "demo-cut" / "media" / "b-roll.mp4").exists())

    # 10. MLT export -------------------------------------------------------
    print("-- MLT export --")
    d, data = video.export_mlt("demo-cut")
    check("mlt export", d.allowed and Path(data["mlt"]).exists(), d.reason)

    # 11. broker down fails closed (service mode, no broker running) --------
    print("-- fail-closed --")
    video_dead = VeilVideo(session="test-1", projects_root=projects,
                           broker_mode="service",
                           broker_run_dir=tmp / "no-such-run-dir")
    d, _ = video_dead.probe_media("demo-cut", "a-roll.mp4")
    check("broker down -> deny, no execution", not d.allowed
          and "fail-closed" in d.reason, d.reason)

    # 12. jobs list ---------------------------------------------------------
    d, jobs = video.list_jobs("demo-cut")
    check("jobs listed", d.allowed and len(jobs) >= 3, str(len(jobs) if d.allowed else d.reason))

    print(f"\n{ PASS} passed, {FAIL} failed  (workdir: {tmp})")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
