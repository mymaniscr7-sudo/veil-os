"""
VEIL Video — probe: structured media understanding via ffprobe.

The AI never watches a video to learn what it is. It reads this:
duration, resolution, fps, codecs, audio streams, bitrate — compact
JSON it can reason about, plus scene boundaries and a thumbnail
contact sheet it can actually look at when it needs to.

ffprobe is the source of truth for durations. Timeline validation
in project.py uses these numbers, so a clip can never claim to be
longer than its source.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

FFPROBE = "ffprobe"
FFMPEG = "ffmpeg"


class ProbeError(Exception):
    pass


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise ProbeError(f"{cmd[0]} not found on PATH: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise ProbeError(f"{cmd[0]} timed out after {timeout}s") from e


def probe(path: Path) -> dict:
    """ffprobe -> compact structured metadata. Raises ProbeError."""
    path = Path(path)
    if not path.is_file():
        raise ProbeError(f"no such file: {path}")
    r = _run([FFPROBE, "-v", "error",
              "-show_entries",
              "stream=width,height,r_frame_rate,avg_frame_rate,codec_name,duration",
              "-show_entries", "format=duration,size,bit_rate,format_name",
              "-show_entries", "stream=codec_type,codec_name,channels,sample_rate",
              "-of", "json", str(path)])
    if r.returncode != 0:
        raise ProbeError(f"ffprobe failed on {path.name}: {r.stderr.strip()[:300]}")
    raw = json.loads(r.stdout or "{}")
    video = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = [s for s in raw.get("streams", []) if s.get("codec_type") == "audio"]
    fmt = raw.get("format", {})

    def _fps(s: dict | None) -> float | None:
        if not s:
            return None
        for key in ("r_frame_rate", "avg_frame_rate"):
            v = s.get(key) or ""
            if "/" in v:
                num, den = v.split("/", 1)
                try:
                    if float(den):
                        return round(float(num) / float(den), 3)
                except ValueError:
                    pass
        return None

    duration = None
    for v in (video.get("duration") if video else None, fmt.get("duration")):
        try:
            duration = float(v)
            break
        except (TypeError, ValueError):
            continue
    return {
        "file": path.name,
        "duration_seconds": duration,
        "format": fmt.get("format_name"),
        "size_bytes": int(fmt["size"]) if fmt.get("size") else None,
        "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "video": None if video is None else {
            "codec": video.get("codec_name"),
            "width": video.get("width"), "height": video.get("height"),
            "fps": _fps(video),
        },
        "audio": [{
            "codec": s.get("codec_name"),
            "channels": s.get("channels"),
            "sample_rate": int(s["sample_rate"]) if s.get("sample_rate") else None,
        } for s in audio],
        "has_video": video is not None,
        "has_audio": bool(audio),
    }


def detect_scenes(path: Path, threshold: float = 0.4) -> dict:
    """ffmpeg scene detection: timestamps where the picture changes
    sharply. Returns a list of {time, score}. This is how the AI
    finds cuts / shots without watching the whole clip."""
    path = Path(path)
    if not path.is_file():
        raise ProbeError(f"no such file: {path}")
    # select filter prints pts_time for frames above the threshold.
    r = _run([FFMPEG, "-v", "info", "-i", str(path), "-vf",
              f"select='gt(scene,{threshold})',showinfo",
              "-f", "null", "-"], timeout=300)
    import re
    scenes: list[dict] = [{"time": 0.0, "score": 1.0}]
    for m in re.finditer(r"pts_time:([0-9.]+)", r.stderr or ""):
        try:
            scenes.append({"time": round(float(m.group(1)), 3), "score": None})
        except ValueError:
            pass
    # Dedupe + sort (showinfo can repeat a timestamp).
    seen: set[float] = set()
    uniq = []
    for s in sorted(scenes, key=lambda s: s["time"]):
        if s["time"] not in seen:
            seen.add(s["time"])
            uniq.append(s)
    return {"file": path.name, "threshold": threshold,
            "scene_count": len(uniq), "scenes": uniq}


def contact_sheet(path: Path, out: Path, thumbs: int = 8,
                  thumb_w: int = 320) -> Path:
    """N evenly-spaced thumbnails tiled into one image. The AI looks
    at ONE image to understand a whole clip instead of scrubbing.
    Returns the sheet path."""
    meta = probe(path)
    duration = meta.get("duration_seconds") or 0
    if duration <= 0:
        raise ProbeError(f"cannot make a contact sheet: no duration for {path.name}")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    times = [round(duration * (i + 0.5) / thumbs, 3) for i in range(thumbs)]
    # Extract thumbs, then tile 4 across.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        inputs: list[str] = []
        for i, t in enumerate(times):
            thumb = tmp / f"t{i:02d}.jpg"
            r = _run([FFMPEG, "-v", "error", "-ss", str(t), "-i", str(path),
                      "-frames:v", "1", "-vf", f"scale={thumb_w}:-1",
                      "-y", str(thumb)], timeout=60)
            if r.returncode == 0 and thumb.exists():
                inputs.append(str(thumb))
        if not inputs:
            raise ProbeError(f"could not extract any thumbnails from {path.name}")
        cols = 4
        rows = (len(inputs) + cols - 1) // cols
        r = _run([FFMPEG, "-v", "error", "-i", "|".join(inputs),
                  "-filter_complex",
                  f"tile={cols}x{rows}",
                  "-y", str(out)], timeout=60)
        # The concat demuxer form above is fragile across ffmpeg
        # versions; fall back to a simple xstack-free montage via
        # multiple -i inputs if the pipe form failed.
        if r.returncode != 0 or not out.exists():
            args = [FFMPEG, "-v", "error"]
            for inp in inputs:
                args += ["-i", inp]
            filt = "".join(f"[{i}:v]scale={thumb_w}:-1[v{i}];" for i in range(len(inputs)))
            filt += "".join(f"[v{i}]" for i in range(len(inputs)))
            filt += f"xstack=inputs={len(inputs)}:layout=" + "|".join(
                f"{(i % cols) * thumb_w}_{(i // cols) * 180}" for i in range(len(inputs)))
            r = _run([*args, "-filter_complex", filt, "-y", str(out)], timeout=60)
        if r.returncode != 0 or not out.exists():
            raise ProbeError(f"contact sheet failed for {path.name}: {r.stderr.strip()[:300]}")
    return out


def make_proxy(path: Path, out: Path, width: int = 640) -> Path:
    """Low-res proxy for fast AI inspection / preview. Same content,
    a fraction of the decode cost."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    r = _run([FFMPEG, "-v", "error", "-i", str(path),
              "-vf", f"scale={width}:-2", "-c:v", "libx264", "-preset", "veryfast",
              "-crf", "28", "-c:a", "aac", "-b:a", "96k", "-y", str(out)],
             timeout=600)
    if r.returncode != 0:
        raise ProbeError(f"proxy failed for {path.name}: {r.stderr.strip()[:300]}")
    return out
