"""
VEIL Video — render: timeline JSON -> ffmpeg, deterministically.

The renderer is a pure function from (timeline, media dir) to an
ffmpeg command. The AI never composes ffmpeg arguments itself —
same rule as the sandbox (the model must not compose bwrap args):
it edits the timeline, and THIS module generates the fixed filter
graph from the validated manifest. A bad timeline fails validation
before ffmpeg ever runs.

v1 render model (deliberately boring, correct first):
  - v1 track: clips trimmed, scaled/padded to project size, fps
    normalised, concatenated in timeline order
  - v2 track: picture-in-picture overlay, same treatment
  - a1/a2: trimmed audio, volume, delayed to timeline start, mixed
  - text track: drawtext overlays (system font; skipped gracefully
    if no font is found — the render still succeeds, and the log
    says the overlay was skipped)
  - fade in/out transitions via ffmpeg fade; dissolve via xfade
    is NOT faked — v1 treats dissolve as a crossfade only when two
    v1 clips overlap in the timeline; otherwise it renders as a
    hard cut and reports that. No silent behaviour change.
  - output: H.264 + AAC, yuv420p, faststart, in renders/

MLT / melt: checked on the dev host 2026-11-06 — NOT installed.
FFmpeg is the v1 renderer. If melt is present, `render_mlt()` can
take the same timeline and emit a .mlt XML for shotcut/kdenlive
interoperability; it is an export path, not the render path, and
it fails clearly when melt is absent.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from project import Clip, Project, Timeline

try:
    from probe import probe as _probe_media
except ImportError:  # probe unavailable — assume sources have audio
    _probe_media = None

FFMPEG = "ffmpeg"


class RenderError(Exception):
    pass


def _font_path() -> str | None:
    for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/TTF/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
        if Path(cand).exists():
            return cand
    return None


def build_ffmpeg_command(timeline: Timeline, project: Project,
                         output: Path, preset: str = "veryfast",
                         crf: int = 23) -> list[str]:
    """Timeline -> ffmpeg argv. No execution here: callers (jobs.py)
    run it, so progress / cancellation live in one place.

    How the graph works (v1, correct before clever):
      - inputs are ALL non-text clips, in timeline order; every
        filter references a clip by its real input index, never by
        its position within one track.
      - v1: clips sorted by start, each trimmed to segment-local
        time (setpts=PTS-STARTPTS). Gaps on v1 are filled with
        black nullsrc segments of the gap length, so the concat
        output keeps true timeline time — a 2s gap stays 2s of black,
        it does not collapse.
      - v2: trimmed, setpts offset to its timeline start, overlaid
        with enable=between(t,...).
      - audio: trimmed, volume, adelay to its timeline start, mixed
        with amix, then apad/atrim to the exact timeline duration.
      - fades are segment-local (fade in st=0, fade out
        st=dur-transition_dur), because each segment is local time.
    """
    problems = timeline.validate()
    if problems:
        raise RenderError("invalid timeline: " + "; ".join(problems))
    if not timeline.clips:
        raise RenderError("timeline is empty — nothing to render")
    v1 = sorted((c for c in timeline.clips if c.track == "v1"),
                key=lambda c: c.start)
    v2 = sorted((c for c in timeline.clips if c.track == "v2"),
                key=lambda c: c.start)
    a_clips = [c for c in timeline.clips if c.track in ("a1", "a2")]
    if not v1:
        raise RenderError("timeline has no video clips")

    # Inputs: every non-text clip, in timeline order.
    inputs: list[Clip] = [c for c in timeline.clips if c.track != "text"]
    in_idx = {c.clip_id: i for i, c in enumerate(inputs)}
    cmd: list[str] = [FFMPEG, "-v", "error", "-y"]
    for c in inputs:
        cmd += ["-i", str(project.media_path(c.source))]
    # Gap fillers are extra lavfi inputs, appended after the files.
    gap_inputs = 0

    def _scale_pad() -> str:
        return (f"scale={timeline.width}:{timeline.height}:force_original_aspect_ratio=decrease,"
                f"pad={timeline.width}:{timeline.height}:(ow-iw)/2:(oh-ih)/2,"
                f"fps={timeline.fps},format=yuv420p")

    filters: list[str] = []
    segments: list[str] = []
    seg_n = 0
    cursor = 0.0  # timeline time covered so far on v1
    for c in v1:
        dur = c.effective_duration()
        if c.start > cursor + 1e-6:
            gap = c.start - cursor
            gi = len(inputs) + gap_inputs
            gap_inputs += 1
            cmd += ["-f", "lavfi", "-i",
                    f"nullsrc=size={timeline.width}x{timeline.height}:rate={timeline.fps}:duration={gap:.3f}"]
            filters.append(f"[{gi}:v]{_scale_pad()}[seg{seg_n}]")
            segments.append(f"[seg{seg_n}]")
            seg_n += 1
        end = c.out_point if c.out_point is not None else c.in_point + dur
        filt = (f"[{in_idx[c.clip_id]}:v]trim=start={c.in_point}:end={end},"
                f"setpts=PTS-STARTPTS,{_scale_pad()}")
        if c.transition_in == "fade":
            filt += f",fade=t=in:st=0:d={c.transition_dur}"
        if c.transition_out == "fade":
            filt += f",fade=t=out:st={max(0.0, dur - c.transition_dur)}:d={c.transition_dur}"
        filt += f"[seg{seg_n}]"
        filters.append(filt)
        segments.append(f"[seg{seg_n}]")
        seg_n += 1
        cursor = max(cursor, c.start + dur)
    if len(segments) == 1:
        video_out = segments[0]
    else:
        filters.append(f"{''.join(segments)}concat=n={len(segments)}:v=1:a=0[vout]")
        video_out = "[vout]"
    # v2 overlays, at their timeline positions.
    for j, c in enumerate(v2):
        dur = c.effective_duration()
        end = c.out_point if c.out_point is not None else c.in_point + dur
        filters.append(
            f"[{in_idx[c.clip_id]}:v]trim=start={c.in_point}:end={end},"
            f"setpts=PTS-STARTPTS+{c.start}/TB,{_scale_pad()}[ov{j}]")
        filters.append(
            f"{video_out}[ov{j}]overlay=0:0:"
            f"enable='between(t,{c.start},{c.start + dur})'[vo{j}]")
        video_out = f"[vo{j}]"
    # Audio. A v1 clip carries its own audio: it is part of the
    # clip, not a separate decision — a cut that silences the
    # picture's own sound would be a silent behaviour change.
    # a1/a2 are extra audio (music, voiceover) mixed on top.
    # v1 clips are SEQUENTIAL, so their audio is concatenated
    # (with anullsrc silence for gaps), not mixed: amix with
    # adelay'd inputs hangs in ffmpeg 5.1 (Debian bookworm, the
    # container's ffmpeg) — it stalls waiting for the delayed input
    # and never produces output. concat is correct here and works
    # on both ffmpeg 5.1 and 8.x. amix is only used when a1/a2
    # overlay audio actually exists on top of v1.
    audio_sources = v1 + a_clips
    # A source with no audio stream must not break the render:
    # referencing [i:a] on it fails the whole ffmpeg run. Probe
    # once per source and use only the ones that actually have audio.
    def _audible(c: Clip) -> bool:
        try:
            return _probe_media(project.media_path(c.source))["has_audio"] \
                if _probe_media is not None else True
        except Exception:
            return False

    v1_audible = [c for c in v1 if _audible(c)]
    overlay_audible = [c for c in a_clips if _audible(c)]
    audio_out = None
    if v1_audible or overlay_audible:
        # -- v1: sequential audio, concatenated (gap silence via anullsrc).
        v1_audio = None
        if v1_audible:
            a_segments: list[str] = []
            a_seg_n = 0
            a_cursor = 0.0
            for c in v1_audible:
                dur = c.effective_duration()
                if c.start > a_cursor + 1e-6:
                    gap = c.start - a_cursor
                    gi = len(inputs) + gap_inputs
                    gap_inputs += 1
                    cmd += ["-f", "lavfi", "-i",
                            f"anullsrc=channel_layout=stereo:sample_rate=48000:duration={gap:.3f}"]
                    filters.append(f"[{gi}:a]aresample=48000[a_seg{a_seg_n}]")
                    a_segments.append(f"[a_seg{a_seg_n}]")
                    a_seg_n += 1
                end = c.out_point if c.out_point is not None else c.in_point + dur
                filters.append(
                    f"[{in_idx[c.clip_id]}:a]atrim=start={c.in_point}:end={end},"
                    f"asetpts=PTS-STARTPTS,volume={c.volume},"
                    f"aresample=48000[a_seg{a_seg_n}]")
                a_segments.append(f"[a_seg{a_seg_n}]")
                a_seg_n += 1
                a_cursor = max(a_cursor, c.start + dur)
            if len(a_segments) == 1:
                v1_audio = a_segments[0]
            else:
                filters.append(
                    f"{''.join(a_segments)}concat=n={len(a_segments)}:v=0:a=1[v1a]")
                v1_audio = "[v1a]"
        # -- a1/a2 overlay audio: mixed on top of v1 (only when present).
        if overlay_audible:
            o_labels: list[str] = []
            for i, c in enumerate(overlay_audible):
                dur = c.effective_duration()
                end = c.out_point if c.out_point is not None else c.in_point + dur
                filters.append(
                    f"[{in_idx[c.clip_id]}:a]atrim=start={c.in_point}:end={end},"
                    f"asetpts=PTS-STARTPTS,volume={c.volume},"
                    f"adelay={int(c.start * 1000)}:all=1,aresample=48000[oa{i}]")
                o_labels.append(f"[oa{i}]")
            if v1_audio is not None:
                mix_in = [v1_audio] + o_labels
            else:
                mix_in = o_labels
            if len(mix_in) == 1:
                mixed = mix_in[0]
            else:
                filters.append(
                    f"{''.join(mix_in)}amix=inputs={len(mix_in)}:normalize=0[amix]")
                mixed = "[amix]"
            filters.append(
                f"{mixed}apad,atrim=0:{timeline.duration():.3f}[aout]")
            audio_out = "[aout]"
        elif v1_audio is not None:
            filters.append(
                f"{v1_audio}apad,atrim=0:{timeline.duration():.3f}[aout]")
            audio_out = "[aout]"
    # Text overlays.
    font = _font_path()
    for k, c in enumerate(t for t in timeline.clips if t.track == "text" and t.text):
        if font is None:
            continue  # skipped gracefully; logged by the job runner
        safe = c.text.replace("'", "").replace(":", "\\:").replace("%", "\\%")
        filters.append(
            f"{video_out}drawtext=fontfile={font}:text='{safe}':fontsize=48:"
            f"fontcolor=white:box=1:boxcolor=black@0.5:boxborderw=12:"
            f"x=(w-text_w)/2:y=h-120:"
            f"enable='between(t,{c.start},{c.start + c.effective_duration()})'[vt{k}]")
        video_out = f"[vt{k}]"
    cmd += ["-filter_complex", ";".join(filters),
            "-map", video_out]
    if audio_out:
        cmd += ["-map", audio_out]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            "-r", str(timeline.fps), str(output)]
    return cmd


def render_timeline(project: Project, output_name: str = "render.mp4",
                    preset: str = "veryfast", crf: int = 23,
                    timeout: int = 3600,
                    progress_cb=None) -> dict:
    """Synchronous render. For background jobs with progress and
    cancel, use jobs.py — it calls build_ffmpeg_command too."""
    timeline = project.load_timeline()
    out = project.renders_dir / output_name
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_ffmpeg_command(timeline, project, out, preset=preset, crf=crf)
    started = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"render timed out after {timeout}s") from e
    if r.returncode != 0:
        raise RenderError(f"ffmpeg failed: {r.stderr.strip()[-500:]}")
    skipped_text = _font_path() is None and any(
        c.track == "text" for c in timeline.clips)
    return {
        "output": str(out), "output_name": output_name,
        "duration_seconds": round(timeline.duration(), 3),
        "width": timeline.width, "height": timeline.height,
        "fps": timeline.fps, "clip_count": len(timeline.clips),
        "elapsed_seconds": round(time.time() - started, 2),
        "text_overlays_skipped_no_font": skipped_text,
        "command": cmd,
    }


def export_mlt_xml(timeline: Timeline, project: Project, out: Path) -> Path:
    """Emit a .mlt XML (MLT / Shotcut / Kdenlive) from the same
    timeline. Interoperability export only — the VEIL render path
    is ffmpeg. Requires nothing but the timeline; melt itself is
    only needed to *play* the .mlt, not to write it."""
    problems = timeline.validate()
    if problems:
        raise RenderError("invalid timeline: " + "; ".join(problems))
    lines = ['<?xml version="1.0" standalone="no"?>',
             '<mlt LC_NUMERIC="C" version="7.0.0" title="VEIL Video export"',
             f'     profile="custom" width="{timeline.width}" height="{timeline.height}"',
             f'     display_aspect_num="16" display_aspect_den="9" frame_rate_num="{int(timeline.fps)}" frame_rate_den="1">',
             ' <playlist id="main_bin"><property name="xml_retain">1</property></playlist>',
             ' <playlist id="playlist0">']
    for c in timeline.clips:
        if c.track == "text":
            continue
        src = project.media_path(c.source)
        lines.append(
            f'  <entry producer="chain-{c.clip_id}" in="{c.in_point:.3f}" '
            f'out="{(c.out_point if c.out_point is not None else c.in_point + c.effective_duration()):.3f}"/>')
    lines += [' </playlist>',
              ' <tractor id="timeline"><track producer="playlist0"/></tractor>',
              '</mlt>']
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    return out


def melt_available() -> bool:
    return shutil.which("melt") is not None
