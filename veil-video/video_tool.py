"""
VEIL Video — the AI's video editor is a CLI/API, not a GUI.

Same idea as the browser tool, applied to video:
  - An AI scrubbing a timeline in a GUI editor is slow, token-hungry
    and fragile. An AI reading probe JSON, a transcript with word
    timestamps and a timeline JSON — then editing that JSON — is
    fast, cheap and deterministic.
  - `probe` to see, `transcript` to hear, `timeline` to edit.
    No playhead, no pixels, no dragging.
  - The AI never composes ffmpeg arguments. It edits the timeline;
    render.py generates the fixed filter graph from the validated
    manifest (same rule as the sandbox: the model never composes
    bwrap args).

How it is gated (the OS part):
  - Every operation proposes to the broker FIRST, with the exact
    action, target, expected effect and a scoped capability.
  - video.read (probe, timeline read, transcript read, job status):
    automatic inside the project.
  - video.edit / video.render / video.transcribe: reversible-write
    inside the project workspace, with snapshot/rollback. Automatic
    once the capability is allowed — the output never leaves the
    project on its own.
  - video.export / video.publish: external side effect — the file
    leaves the project. Needs user confirmation in the Security
    Center (or the Windows Remote API) every time.
  - video.delete: irreversible/privileged when risk is critical
    (deleting source media). Needs explicit confirmation + step-up.
  - One project per task, isolated workspace. Media is COPIED into
    media/ on import — the AI never edits a host path in place.
  - Broker down -> fail closed. Nothing renders, nothing publishes.

Long operations (render, transcribe, scene detection) are jobs:
the method returns a job_id immediately and the work runs in a
background thread. The Windows client polls the job; the AI polls
job_status. Nothing blocks the agent loop for a 10-minute render.
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "veil-core"))

from broker_client import BrokerClient, BrokerUnavailable  # noqa: E402
from jobs import JobRegistry, run_function_job, run_render_job, start_thread  # noqa: E402
from project import Clip, Project, Timeline, validate_project_id  # noqa: E402
import probe as probe_mod  # noqa: E402
import render as render_mod  # noqa: E402
import transcribe as transcribe_mod  # noqa: E402

DEFAULT_PROJECTS_ROOT = HERE / "projects"


@dataclass
class VideoDecision:
    allowed: bool
    outcome: str
    reason: str


class VeilVideo:
    def __init__(self, session: str, projects_root: Path | None = None,
                 broker_run_dir: Path | None = None,
                 broker_mode: str = "service",
                 policy_path: Path | None = None,
                 timeout: int = 30):
        self.session = session
        self.projects_root = Path(projects_root or DEFAULT_PROJECTS_ROOT)
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.broker_mode = broker_mode
        self._inprocess_broker = None
        if broker_mode == "inprocess":
            # Tests / offline demo only. The service is the real path.
            from broker_v2 import BrokerV2  # noqa: E402
            from capability import Capability  # noqa: E402
            policy = policy_path or (HERE.parent / "veil-core" / "policy_v2.example.json")
            self._inprocess_broker = BrokerV2(policy)
            self._Capability = Capability
        elif broker_mode == "service":
            self.broker = BrokerClient(run_dir=broker_run_dir) if broker_run_dir else BrokerClient()
        else:
            raise ValueError(f"unknown broker_mode {broker_mode!r}")

    # -- broker ---------------------------------------------------------
    def _cap(self, capability: str, project_id: str, risk: str = "medium",
             extra_scope: dict | None = None) -> dict:
        scope = {"project": project_id,
                 "paths": [f"projects/{project_id}/**"]}
        if extra_scope:
            scope.update(extra_scope)
        return {"capability": capability, "who": f"agent:{self.session}",
                "scope": scope, "session": self.session, "risk": risk}

    def _propose(self, action: str, target: str, effect: str,
                 capability: str, project_id: str, risk: str = "medium",
                 extra_scope: dict | None = None,
                 approval_token: str | None = None) -> VideoDecision:
        cap = self._cap(capability, project_id, risk, extra_scope)
        try:
            if self._inprocess_broker is not None:
                from capability import Capability  # noqa: E402
                c = Capability(capability=cap["capability"], who=cap["who"],
                               scope=cap["scope"], session=cap["session"],
                               risk=cap["risk"])
                r = self._inprocess_broker.propose(
                    action=action, target=target, expected_effect=effect,
                    capability=c, approval_token=approval_token)
                return VideoDecision(r.outcome == "allow", r.outcome, r.reason)
            r = self.broker.propose(action=action, target=target,
                                    expected_effect=effect, capability=cap,
                                    approval_token=approval_token)
        except BrokerUnavailable as e:
            return VideoDecision(False, "deny", f"fail-closed: broker unavailable: {e}")
        return VideoDecision(r["outcome"] == "allow", r["outcome"], r["reason"])

    def _jobs(self, project_id: str) -> JobRegistry:
        return JobRegistry(self.projects_root / project_id / "jobs.json")

    def _project(self, project_id: str) -> Project:
        return Project.open(self.projects_root, project_id)

    # -- projects (read) --------------------------------------------------
    def list_projects(self):
        d = self._propose("video.read", "projects", "list video projects",
                          "video.read", "*", risk="low")
        if not d.allowed:
            return d, None
        return d, Project.list(self.projects_root)

    def create_project(self, project_id: str | None = None, name: str = "",
                       width: int = 1920, height: int = 1080,
                       fps: float = 30.0):
        pid = project_id or ""
        d = self._propose("video.edit", f"project {pid or '(new)'}",
                          f"create video project {name or pid}",
                          "video.edit", pid or "*", risk="low")
        if not d.allowed:
            return d, None
        try:
            proj = Project.create(self.projects_root, project_id or None,
                                  name=name, width=width, height=height, fps=fps,
                                  task=self.session)
        except (ValueError, FileExistsError) as e:
            return VideoDecision(False, "deny", str(e)), None
        return d, {"project_id": proj.project_id, **proj.manifest()}

    def project_summary(self, project_id: str):
        d = self._propose("video.read", f"project {project_id}",
                          f"read project {project_id} summary",
                          "video.read", project_id, risk="low")
        if not d.allowed:
            return d, None
        try:
            proj = self._project(project_id)
        except (ValueError, FileNotFoundError) as e:
            return VideoDecision(False, "deny", str(e)), None
        manifest = proj.manifest()
        tl = proj.load_timeline()
        return d, {**manifest, "clip_count": len(tl.clips),
                   "duration_seconds": round(tl.duration(), 3),
                   "transcript": proj.read_sidecar("transcript.json") is not None,
                   "scenes": proj.read_sidecar("scenes.json") is not None}

    # -- media --------------------------------------------------------------
    def import_media(self, project_id: str, src: str, dest_name: str | None = None,
                     approval_token: str | None = None):
        d = self._propose("video.edit", f"{project_id}/media/{dest_name or Path(src).name}",
                          f"import {src} into project {project_id}",
                          "video.edit", project_id, risk="low",
                          approval_token=approval_token)
        if not d.allowed:
            return d, None
        try:
            proj = self._project(project_id)
            dest = proj.import_media(Path(src), dest_name)
            meta = probe_mod.probe(dest)
        except (ValueError, FileNotFoundError, probe_mod.ProbeError) as e:
            return VideoDecision(False, "deny", str(e)), None
        return d, meta

    def probe_media(self, project_id: str, filename: str):
        d = self._propose("video.read", f"{project_id}/media/{filename}",
                          f"probe {filename} in project {project_id}",
                          "video.read", project_id, risk="low")
        if not d.allowed:
            return d, None
        try:
            meta = probe_mod.probe(self._project(project_id).media_path(filename))
        except (ValueError, FileNotFoundError, probe_mod.ProbeError) as e:
            return VideoDecision(False, "deny", str(e)), None
        return d, meta

    def contact_sheet(self, project_id: str, filename: str, thumbs: int = 8):
        d = self._propose("video.edit", f"{project_id}/media/{filename}",
                          f"make contact sheet for {filename}",
                          "video.edit", project_id, risk="low")
        if not d.allowed:
            return d, None
        try:
            proj = self._project(project_id)
            out = proj.root / "proxies" / f"{Path(filename).stem}-sheet.jpg"
            probe_mod.contact_sheet(proj.media_path(filename), out, thumbs=thumbs)
        except (ValueError, FileNotFoundError, probe_mod.ProbeError) as e:
            return VideoDecision(False, "deny", str(e)), None
        return d, {"contact_sheet": str(out), "thumbs": thumbs}

    # -- timeline -------------------------------------------------------------
    def get_timeline(self, project_id: str):
        d = self._propose("video.read", f"{project_id}/timeline.json",
                          f"read timeline of project {project_id}",
                          "video.read", project_id, risk="low")
        if not d.allowed:
            return d, None
        try:
            return d, self._project(project_id).load_timeline().to_dict()
        except (ValueError, FileNotFoundError) as e:
            return VideoDecision(False, "deny", str(e)), None

    def _edit_timeline(self, project_id: str, action: str, effect: str,
                       mutate, approval_token: str | None = None):
        d = self._propose("video.edit", f"{project_id}/timeline.json", effect,
                          "video.edit", project_id, risk="low",
                          approval_token=approval_token)
        if not d.allowed:
            return d, None
        try:
            proj = self._project(project_id)
            tl = proj.load_timeline()
            mutate(tl)
            # Validate against probed source durations when available.
            durations = {}
            for clip in tl.clips:
                if clip.track == "text":
                    continue
                try:
                    durations[clip.source] = probe_mod.probe(
                        proj.media_path(clip.source))["duration_seconds"]
                except Exception:
                    pass
            problems = tl.validate(durations or None)
            if problems:
                return VideoDecision(False, "deny",
                                     "invalid timeline: " + "; ".join(problems)), None
            proj.save_timeline(tl)
        except (ValueError, FileNotFoundError) as e:
            return VideoDecision(False, "deny", str(e)), None
        return d, tl.to_dict()

    def add_clip(self, project_id: str, clip: dict, approval_token: str | None = None):
        try:
            c = Clip.from_dict(clip)
        except (KeyError, TypeError, ValueError) as e:
            return VideoDecision(False, "deny", f"bad clip: {e}"), None

        def _add(tl: Timeline):
            tl.clips.append(c)
        return self._edit_timeline(
            project_id, "video.edit",
            f"add clip {c.clip_id} ({c.source}) to project {project_id}",
            _add, approval_token)

    def remove_clip(self, project_id: str, clip_id: str, approval_token: str | None = None):
        def _remove(tl: Timeline):
            before = len(tl.clips)
            tl.clips = [c for c in tl.clips if c.clip_id != clip_id]
            if len(tl.clips) == before:
                raise ValueError(f"no clip {clip_id} in timeline")
        return self._edit_timeline(
            project_id, "video.edit", f"remove clip {clip_id} from {project_id}",
            _remove, approval_token)

    def move_clip(self, project_id: str, clip_id: str, new_start: float,
                  approval_token: str | None = None):
        def _move(tl: Timeline):
            for c in tl.clips:
                if c.clip_id == clip_id:
                    c.start = float(new_start)
                    return
            raise ValueError(f"no clip {clip_id} in timeline")
        return self._edit_timeline(
            project_id, "video.edit",
            f"move clip {clip_id} to {new_start}s in {project_id}",
            _move, approval_token)

    def trim_clip(self, project_id: str, clip_id: str, in_point: float,
                  out_point: float, approval_token: str | None = None):
        def _trim(tl: Timeline):
            for c in tl.clips:
                if c.clip_id == clip_id:
                    c.in_point = float(in_point)
                    c.out_point = float(out_point)
                    c.duration = float(out_point) - float(in_point)
                    return
            raise ValueError(f"no clip {clip_id} in timeline")
        return self._edit_timeline(
            project_id, "video.edit",
            f"trim clip {clip_id} to {in_point}-{out_point}s in {project_id}",
            _trim, approval_token)

    # -- understanding: transcript + scenes (jobs) -----------------------------
    def transcribe(self, project_id: str, filename: str, model: str = "base",
                   approval_token: str | None = None):
        """Start a transcription job. Returns (decision, job). The
        transcript lands in transcript.json when the job finishes."""
        d = self._propose("video.transcribe", f"{project_id}/media/{filename}",
                          f"transcribe {filename} in project {project_id}",
                          "video.transcribe", project_id, risk="low",
                          approval_token=approval_token)
        if not d.allowed:
            return d, None
        try:
            proj = self._project(project_id)
            media = proj.media_path(filename)
        except (ValueError, FileNotFoundError) as e:
            return VideoDecision(False, "deny", str(e)), None
        reg = self._jobs(project_id)
        job = reg.create("transcribe", project_id, label=f"transcribe {filename}")

        def _work():
            def _fn():
                result = transcribe_mod.transcribe(media, model=model)
                proj.write_sidecar("transcript.json", result)
                return {"transcript": "transcript.json",
                        "words": len(result.get("words", [])),
                        "segments": len(result.get("segments", []))}
            run_function_job(reg, job["job_id"], _fn)
        start_thread(_work)
        return d, job

    def transcript(self, project_id: str):
        d = self._propose("video.read", f"{project_id}/transcript.json",
                          f"read transcript of project {project_id}",
                          "video.read", project_id, risk="low")
        if not d.allowed:
            return d, None
        try:
            data = self._project(project_id).read_sidecar("transcript.json")
        except (ValueError, FileNotFoundError) as e:
            return VideoDecision(False, "deny", str(e)), None
        if data is None:
            return VideoDecision(False, "deny",
                                 "no transcript yet — run transcribe first"), None
        return d, data

    def find_in_transcript(self, project_id: str, query: str):
        d, data = self.transcript(project_id)
        if not d.allowed or data is None:
            return d, None
        return d, transcribe_mod.search_transcript(data, query)

    def detect_scenes(self, project_id: str, filename: str,
                      threshold: float = 0.4, approval_token: str | None = None):
        d = self._propose("video.transcribe", f"{project_id}/media/{filename}",
                          f"detect scenes in {filename} in project {project_id}",
                          "video.transcribe", project_id, risk="low",
                          approval_token=approval_token)
        if not d.allowed:
            return d, None
        try:
            proj = self._project(project_id)
            media = proj.media_path(filename)
        except (ValueError, FileNotFoundError) as e:
            return VideoDecision(False, "deny", str(e)), None
        reg = self._jobs(project_id)
        job = reg.create("scenes", project_id, label=f"scenes {filename}")

        def _work():
            def _fn():
                result = probe_mod.detect_scenes(media, threshold=threshold)
                proj.write_sidecar("scenes.json", result)
                return {"scenes": "scenes.json",
                        "scene_count": result["scene_count"]}
            run_function_job(reg, job["job_id"], _fn)
        start_thread(_work)
        return d, job

    # -- render (job) -------------------------------------------------------------
    def render(self, project_id: str, output_name: str = "render.mp4",
               preset: str = "veryfast", crf: int = 23,
               approval_token: str | None = None):
        d = self._propose("video.render", f"{project_id}/renders/{output_name}",
                          f"render project {project_id} to {output_name}",
                          "video.render", project_id, risk="medium",
                          approval_token=approval_token)
        if not d.allowed:
            return d, None
        try:
            proj = self._project(project_id)
            tl = proj.load_timeline()
            out = proj.renders_dir / output_name
            cmd = render_mod.build_ffmpeg_command(tl, proj, out,
                                                  preset=preset, crf=crf)
        except (ValueError, FileNotFoundError, render_mod.RenderError) as e:
            return VideoDecision(False, "deny", str(e)), None
        reg = self._jobs(project_id)
        job = reg.create("render", project_id, label=f"render {output_name}",
                         total_seconds=tl.duration())
        start_thread(run_render_job, reg, job["job_id"], cmd, out,
                     total_seconds=tl.duration())
        return d, job

    def export_mlt(self, project_id: str, output_name: str = "timeline.mlt"):
        d = self._propose("video.edit", f"{project_id}/exports/{output_name}",
                          f"export MLT timeline for project {project_id}",
                          "video.edit", project_id, risk="low")
        if not d.allowed:
            return d, None
        try:
            proj = self._project(project_id)
            out = render_mod.export_mlt_xml(proj.load_timeline(), proj,
                                            proj.exports_dir / output_name)
        except (ValueError, FileNotFoundError, render_mod.RenderError) as e:
            return VideoDecision(False, "deny", str(e)), None
        return d, {"mlt": str(out)}

    # -- jobs ------------------------------------------------------------------------
    def job_status(self, project_id: str, job_id: str):
        d = self._propose("video.read", f"{project_id}/jobs/{job_id}",
                          f"read job {job_id} in project {project_id}",
                          "video.read", project_id, risk="low")
        if not d.allowed:
            return d, None
        job = self._jobs(project_id).get(job_id)
        if job is None:
            return VideoDecision(False, "deny", f"no job {job_id}"), None
        return d, job

    def list_jobs(self, project_id: str):
        d = self._propose("video.read", f"{project_id}/jobs",
                          f"list jobs in project {project_id}",
                          "video.read", project_id, risk="low")
        if not d.allowed:
            return d, None
        return d, self._jobs(project_id).list(project_id)

    def cancel_job(self, project_id: str, job_id: str):
        d = self._propose("video.edit", f"{project_id}/jobs/{job_id}",
                          f"cancel job {job_id} in project {project_id}",
                          "video.edit", project_id, risk="low")
        if not d.allowed:
            return d, None
        job = self._jobs(project_id).cancel(job_id)
        if job is None:
            return VideoDecision(False, "deny", f"no job {job_id}"), None
        return d, job

    # -- publish / delete (need confirmation / step-up) --------------------------------
    def publish(self, project_id: str, output_name: str, dest: str,
                approval_token: str | None = None):
        """Copy a render OUT of the project to `dest`. External side
        effect: needs user confirmation, every time. The broker
        decides; this method never copies without an allow."""
        d = self._propose("video.publish", dest,
                          f"publish {output_name} from project {project_id} to {dest}",
                          "video.publish", project_id, risk="high",
                          extra_scope={"dest": dest},
                          approval_token=approval_token)
        if not d.allowed:
            return d, None
        try:
            proj = self._project(project_id)
            src = proj.renders_dir / output_name
            if not src.is_file():
                return VideoDecision(False, "deny", f"no render {output_name}"), None
            dest_path = Path(dest)
            if dest_path.is_dir():
                dest_path = dest_path / output_name
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest_path)
        except (ValueError, FileNotFoundError, OSError) as e:
            return VideoDecision(False, "deny", str(e)), None
        return d, {"published": str(dest_path)}

    def delete_media(self, project_id: str, filename: str,
                     approval_token: str | None = None):
        """Delete source media. Irreversible: risk critical, so the
        broker requires explicit confirmation + step-up auth."""
        d = self._propose("video.delete", f"{project_id}/media/{filename}",
                          f"delete source media {filename} from project {project_id}",
                          "video.delete", project_id, risk="critical",
                          approval_token=approval_token)
        if not d.allowed:
            return d, None
        try:
            p = self._project(project_id).media_path(filename)
            p.unlink()
        except (ValueError, FileNotFoundError, OSError) as e:
            return VideoDecision(False, "deny", str(e)), None
        return d, {"deleted": filename}
