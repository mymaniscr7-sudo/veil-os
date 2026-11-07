"""
VEIL Video — transcribe via faster-whisper.

Word-level timestamps are the point: the AI edits video by *words*
("cut everything between 'um' at 12.4s and the next sentence"),
not by scrubbing. The transcript is stored as structured JSON in
the project, with segments and words, so later edits can reference
a word id instead of a raw time.

Honest degradation: faster-whisper is NOT installed on the dev host
yet (checked 2026-11-06), and neither is openai-whisper. If neither
is importable, transcribe() raises TranscribeUnavailable with the
install command — it never invents a transcript. A fake transcript
would be worse than none: the AI would cut on words that were never
said.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

INSTALL_HINT = (
    "faster-whisper is not installed. On Arch: "
    "pacman -S python-faster-whisper  (or pip install faster-whisper). "
    "It needs a model download on first use (tiny/base/small) — "
    "that download is a network action and goes through the broker."
)


class TranscribeUnavailable(Exception):
    pass


class TranscribeError(Exception):
    pass


def backend_available() -> dict:
    """What transcription backends exist on this host, right now."""
    try:
        import faster_whisper  # noqa: F401
        fw = True
    except ImportError:
        fw = False
    return {
        "faster_whisper": fw,
        "whisper_cli": shutil.which("whisper") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }


def transcribe(path: Path, model: str = "base", language: str | None = None,
               word_timestamps: bool = True) -> dict:
    """Transcribe an audio/video file. Returns the transcript dict
    (also what gets stored as transcript.json in the project).

    Uses faster-whisper in-process when available. Model names:
    tiny, base, small, medium, large-v3. `base` is the v1 default:
    fast on CPU, good enough for edit decisions.
    """
    path = Path(path)
    if not path.is_file():
        raise TranscribeError(f"no such file: {path}")
    avail = backend_available()
    if avail["faster_whisper"]:
        return _transcribe_faster_whisper(path, model, language, word_timestamps)
    if avail["whisper_cli"]:
        return _transcribe_whisper_cli(path, model, language, word_timestamps)
    raise TranscribeUnavailable(INSTALL_HINT)


def _transcribe_faster_whisper(path: Path, model: str, language: str | None,
                              word_timestamps: bool) -> dict:
    from faster_whisper import WhisperModel
    try:
        m = WhisperModel(model, device="cpu", compute_type="int8")
        segments, info = m.transcribe(str(path), language=language,
                                      word_timestamps=word_timestamps,
                                      vad_filter=True)
    except Exception as e:
        raise TranscribeError(f"faster-whisper failed on {path.name}: {e}") from e
    out_segments: list[dict] = []
    words: list[dict] = []
    for seg in segments:
        seg_words = []
        for w in (seg.words or []):
            wd = {"word": w.word, "start": round(w.start, 3),
                  "end": round(w.end, 3), "probability": round(w.probability, 3)}
            seg_words.append(wd)
            words.append(wd)
        out_segments.append({
            "id": seg.id, "start": round(seg.start, 3),
            "end": round(seg.end, 3), "text": seg.text.strip(),
            "words": seg_words,
        })
    return {
        "file": path.name, "model": model,
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "duration_seconds": round(info.duration, 3),
        "segments": out_segments, "words": words,
        "text": " ".join(s["text"] for s in out_segments),
    }


def _transcribe_whisper_cli(path: Path, model: str, language: str | None,
                           word_timestamps: bool) -> dict:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cmd = ["whisper", str(path), "--model", model, "--output_format", "json",
               "--output_dir", str(tmp), "--fp16", "False"]
        if language:
            cmd += ["--language", language]
        if word_timestamps:
            cmd += ["--word_timestamps", "True"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise TranscribeError(f"whisper CLI failed: {r.stderr.strip()[:300]}")
        data = json.loads((tmp / (path.stem + ".json")).read_text())
        words = []
        segments = []
        for seg in data.get("segments", []):
            seg_words = [{"word": w["word"], "start": round(w["start"], 3),
                          "end": round(w["end"], 3),
                          "probability": round(w.get("probability", 0), 3)}
                         for w in seg.get("words", [])]
            words.extend(seg_words)
            segments.append({"id": seg["id"], "start": round(seg["start"], 3),
                             "end": round(seg["end"], 3),
                             "text": seg["text"].strip(), "words": seg_words})
        return {"file": path.name, "model": model,
                "language": data.get("language"),
                "language_probability": None,
                "duration_seconds": None,
                "segments": segments, "words": words,
                "text": data.get("text", "").strip()}


def find_silences(words: list[dict], min_gap: float = 0.8) -> list[dict]:
    """Gaps between words — candidate cut points and 'um'-free zones.
    Pure function over a transcript; no model needed."""
    gaps: list[dict] = []
    for a, b in zip(words, words[1:]):
        gap = b["start"] - a["end"]
        if gap >= min_gap:
            gaps.append({"start": round(a["end"], 3), "end": round(b["start"], 3),
                         "duration": round(gap, 3),
                         "after": a["word"], "before": b["word"]})
    return gaps


def search_transcript(transcript: dict, query: str) -> list[dict]:
    """Find every occurrence of a word/phrase with timestamps.
    This is the AI's 'find the moment they said X' primitive."""
    q = query.strip().lower()
    hits: list[dict] = []
    for w in transcript.get("words", []):
        if q in w["word"].lower():
            hits.append(w)
    return hits
