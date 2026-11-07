"""
VEIL Video — project model.

A project is a directory, never a GUI state blob:

    projects/<project-id>/
      project.json      manifest: name, resolution, fps, created, task
      media/            imported source files (copies, never host paths)
      timeline.json     the edit: tracks of clips, transitions, text
      transcript.json   faster-whisper output (word-level, when available)
      scenes.json       ffmpeg scene-detection output
      proxies/          low-res proxies for fast AI inspection
      renders/          rendered outputs (inside the project)
      exports/          staged exports awaiting publish approval
      jobs.json         render / transcribe job registry for this project

The AI edits timeline.json as structured JSON — it never scrubs a
playhead or drags a clip. Every timeline operation is validated here
before anything touches ffmpeg, so a bad edit fails fast with a
clear error instead of producing a corrupt render halfway through.

Rules enforced by this module (broker policy lives one layer up,
in video_tool.py):
  - project ids are [a-z0-9_-]+, no path traversal
  - media paths must resolve inside the project's media/ dir
  - clip in/out must lie inside the source duration (when probed)
  - timeline times are seconds (float), never frames, never strings
  - a render output always lands in renders/ inside the project;
    getting a file OUT of the project is video.export, a broker
    decision, not a project operation
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# v1 timeline schema version. Bump when the JSON shape changes.
TIMELINE_VERSION = 1


def new_project_id() -> str:
    return f"vid-{uuid.uuid4().hex[:8]}"


def validate_project_id(project_id: str) -> str:
    if not PROJECT_ID_RE.match(project_id or ""):
        raise ValueError(
            f"invalid project id {project_id!r}: use lowercase letters, "
            "digits, '-' or '_', max 64 chars")
    return project_id


@dataclass
class Clip:
    """One clip on a track. Times are seconds, floats."""
    clip_id: str
    source: str            # filename inside media/, e.g. "a-roll.mp4"
    track: str             # "v1" | "v2" | "a1" | "a2" | "text"
    start: float           # timeline position
    in_point: float = 0.0  # source offset
    out_point: float | None = None  # source end; None = to end of source
    duration: float | None = None   # timeline duration; None = out-in
    speed: float = 1.0
    volume: float = 1.0
    text: str = ""         # for track == "text": overlay content
    transition_in: str = ""  # "" | "fade" | "dissolve"
    transition_out: str = ""
    transition_dur: float = 0.5

    def effective_duration(self) -> float:
        if self.duration is not None:
            return float(self.duration)
        if self.out_point is not None:
            return float(self.out_point) - float(self.in_point)
        raise ValueError(f"clip {self.clip_id}: duration unknown without out_point")

    def to_dict(self) -> dict:
        return {
            "clip_id": self.clip_id, "source": self.source, "track": self.track,
            "start": self.start, "in_point": self.in_point,
            "out_point": self.out_point, "duration": self.duration,
            "speed": self.speed, "volume": self.volume, "text": self.text,
            "transition_in": self.transition_in,
            "transition_out": self.transition_out,
            "transition_dur": self.transition_dur,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Clip":
        return cls(
            clip_id=d["clip_id"], source=d["source"], track=d["track"],
            start=float(d["start"]), in_point=float(d.get("in_point", 0.0)),
            out_point=(float(d["out_point"]) if d.get("out_point") is not None else None),
            duration=(float(d["duration"]) if d.get("duration") is not None else None),
            speed=float(d.get("speed", 1.0)), volume=float(d.get("volume", 1.0)),
            text=d.get("text", ""),
            transition_in=d.get("transition_in", ""),
            transition_out=d.get("transition_out", ""),
            transition_dur=float(d.get("transition_dur", 0.5)),
        )


@dataclass
class Timeline:
    version: int = TIMELINE_VERSION
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    clips: list[Clip] = field(default_factory=list)

    def duration(self) -> float:
        return max((c.start + c.effective_duration() for c in self.clips),
                   default=0.0)

    def validate(self, durations: dict[str, float] | None = None) -> list[str]:
        """Return a list of problems (empty = valid). `durations` maps
        source filename -> probed duration in seconds, when known."""
        problems: list[str] = []
        seen: set[str] = set()
        for c in self.clips:
            if c.clip_id in seen:
                problems.append(f"duplicate clip_id {c.clip_id}")
            seen.add(c.clip_id)
            if c.track not in ("v1", "v2", "a1", "a2", "text"):
                problems.append(f"clip {c.clip_id}: unknown track {c.track!r}")
            if c.start < 0:
                problems.append(f"clip {c.clip_id}: negative start")
            if c.in_point < 0:
                problems.append(f"clip {c.clip_id}: negative in_point")
            if c.speed <= 0:
                problems.append(f"clip {c.clip_id}: speed must be > 0")
            if not (0.0 <= c.volume <= 2.0):
                problems.append(f"clip {c.clip_id}: volume must be 0..2")
            if c.transition_in not in ("", "fade", "dissolve"):
                problems.append(f"clip {c.clip_id}: bad transition_in")
            if c.transition_out not in ("", "fade", "dissolve"):
                problems.append(f"clip {c.clip_id}: bad transition_out")
            if c.track == "text" and not c.text:
                problems.append(f"clip {c.clip_id}: text track needs text")
            if c.track != "text":
                if "/" in c.source or "\\" in c.source or c.source.startswith("."):
                    problems.append(f"clip {c.clip_id}: source must be a plain filename in media/")
                if durations and c.source in durations:
                    src_dur = durations[c.source]
                    end = c.out_point if c.out_point is not None else src_dur
                    if c.in_point >= src_dur:
                        problems.append(
                            f"clip {c.clip_id}: in_point {c.in_point} beyond source "
                            f"duration {src_dur} ({c.source})")
                    if end > src_dur + 0.05:
                        problems.append(
                            f"clip {c.clip_id}: out_point {end} beyond source "
                            f"duration {src_dur} ({c.source})")
            try:
                c.effective_duration()
            except ValueError as e:
                problems.append(str(e))
        # Overlap check on the main video track only (v2 is overlays).
        v1 = sorted((c for c in self.clips if c.track == "v1"),
                    key=lambda c: c.start)
        for a, b in zip(v1, v1[1:]):
            if b.start < a.start + a.effective_duration() - 1e-6:
                problems.append(
                    f"v1 overlap: {a.clip_id} and {b.clip_id} overlap "
                    f"({b.start} < {a.start + a.effective_duration():.3f})")
        return problems

    def to_dict(self) -> dict:
        return {"version": self.version, "width": self.width,
                "height": self.height, "fps": self.fps,
                "clips": [c.to_dict() for c in self.clips]}

    @classmethod
    def from_dict(cls, d: dict) -> "Timeline":
        return cls(version=int(d.get("version", TIMELINE_VERSION)),
                   width=int(d.get("width", 1920)),
                   height=int(d.get("height", 1080)),
                   fps=float(d.get("fps", 30.0)),
                   clips=[Clip.from_dict(c) for c in d.get("clips", [])])


class Project:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.project_id = self.root.name
        validate_project_id(self.project_id)

    # -- paths ---------------------------------------------------------
    @property
    def manifest_path(self) -> Path:
        return self.root / "project.json"

    @property
    def timeline_path(self) -> Path:
        return self.root / "timeline.json"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def renders_dir(self) -> Path:
        return self.root / "renders"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    # -- lifecycle -----------------------------------------------------
    @classmethod
    def create(cls, projects_root: Path, project_id: str | None = None,
               name: str = "", width: int = 1920, height: int = 1080,
               fps: float = 30.0, task: str = "") -> "Project":
        project_id = validate_project_id(project_id or new_project_id())
        root = Path(projects_root) / project_id
        if root.exists():
            raise FileExistsError(f"project {project_id} already exists")
        for sub in ("media", "proxies", "renders", "exports"):
            (root / sub).mkdir(parents=True)
        proj = cls(root)
        proj.manifest_path.write_text(json.dumps({
            "project_id": project_id, "name": name or project_id,
            "width": width, "height": height, "fps": fps,
            "task": task,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "draft",
        }, indent=2))
        proj.save_timeline(Timeline(width=width, height=height, fps=fps))
        (root / "jobs.json").write_text("[]")
        return proj

    @classmethod
    def open(cls, projects_root: Path, project_id: str) -> "Project":
        root = Path(projects_root) / validate_project_id(project_id)
        if not (root / "project.json").exists():
            raise FileNotFoundError(f"no such project: {project_id}")
        return cls(root)

    @classmethod
    def list(cls, projects_root: Path) -> list[dict]:
        root = Path(projects_root)
        out = []
        if not root.exists():
            return out
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            try:
                manifest = json.loads((d / "project.json").read_text())
            except (OSError, ValueError):
                continue
            tl = cls._timeline_summary(d)
            out.append({**manifest, **tl,
                        "media_count": len(list((d / "media").glob("*"))) if (d / "media").exists() else 0,
                        "render_count": len(list((d / "renders").glob("*.mp4"))) if (d / "renders").exists() else 0})
        return out

    @staticmethod
    def _timeline_summary(project_dir: Path) -> dict:
        try:
            tl = Timeline.from_dict(json.loads((project_dir / "timeline.json").read_text()))
            return {"clip_count": len(tl.clips),
                    "duration_seconds": round(tl.duration(), 3)}
        except (OSError, ValueError):
            return {"clip_count": 0, "duration_seconds": 0.0}

    # -- manifest / timeline -------------------------------------------
    def manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text())

    def set_status(self, status: str) -> dict:
        m = self.manifest()
        m["status"] = status
        m["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.manifest_path.write_text(json.dumps(m, indent=2))
        return m

    def load_timeline(self) -> Timeline:
        return Timeline.from_dict(json.loads(self.timeline_path.read_text()))

    def save_timeline(self, timeline: Timeline) -> Timeline:
        problems = timeline.validate()
        if problems:
            raise ValueError("invalid timeline: " + "; ".join(problems))
        self.timeline_path.write_text(json.dumps(timeline.to_dict(), indent=2))
        return timeline

    # -- media -----------------------------------------------------------
    def import_media(self, src: Path, dest_name: str | None = None) -> Path:
        """Copy a file INTO media/. Never link or reference a host path:
        the project must stay self-contained so it can be snapshotted,
        moved, or rolled back as one unit."""
        src = Path(src)
        if not src.is_file():
            raise FileNotFoundError(f"no such media file: {src}")
        name = dest_name or src.name
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"bad media name {name!r}")
        dest = (self.media_dir / name).resolve()
        if not str(dest).startswith(str(self.media_dir.resolve())):
            raise ValueError("media destination escapes media/")
        shutil.copyfile(src, dest)
        return dest

    def media_path(self, filename: str) -> Path:
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise ValueError(f"bad media filename {filename!r}")
        p = (self.media_dir / filename).resolve()
        if not str(p).startswith(str(self.media_dir.resolve())):
            raise ValueError("media path escapes media/")
        if not p.is_file():
            raise FileNotFoundError(f"no such media in project: {filename}")
        return p

    # -- sidecar JSON (transcript / scenes) ------------------------------
    def write_sidecar(self, name: str, data: dict) -> Path:
        if name not in ("transcript.json", "scenes.json", "analysis.json"):
            raise ValueError(f"unknown sidecar {name!r}")
        p = self.root / name
        p.write_text(json.dumps(data, indent=2))
        return p

    def read_sidecar(self, name: str) -> dict | None:
        p = self.root / name
        if not p.exists():
            return None
        return json.loads(p.read_text())
