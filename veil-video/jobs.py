"""
VEIL Video — jobs: render / transcribe / probe as background jobs.

The Windows client needs to "monitor rendering or transcription
jobs" — so jobs are first-class, durable state, not a subprocess
the AI happens to remember. A job is a row in jobs.json:

  {job_id, type, project_id, status, progress, output, error,
   log_tail, created_at, updated_at, pid}

Statuses: queued | running | done | failed | cancelled.
Progress comes from ffmpeg's -progress output (out_time_ms vs the
timeline duration) for renders; transcribe/probe jobs report
indeterminate progress (running, no percentage) honestly instead
of inventing one.

Jobs run in a thread inside the process that started them (the CLI
`render --background`, the Remote API, or the AI runtime). The
registry file is the shared truth: the Remote API reads it, the
Security Center reads it, the Windows client polls it. Cancelling
a job kills the ffmpeg process group — a render that keeps running
after "cancel" is a bug, not a feature.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("queued", "running", "done", "failed", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRegistry:
    """jobs.json inside a project dir (per-project) or a shared
    registry file. One registry instance per process; writes are
    atomic (tmp + rename) and guarded by a lock."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self.path.write_text("[]")

    def _read(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []

    def _write(self, jobs: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(jobs, indent=2))
        tmp.replace(self.path)

    def create(self, job_type: str, project_id: str, label: str = "",
               total_seconds: float | None = None) -> dict:
        job = {"job_id": f"job-{uuid.uuid4().hex[:8]}", "type": job_type,
               "project_id": project_id, "label": label,
               "status": "queued", "progress": 0.0,
               "total_seconds": total_seconds,
               "output": None, "error": None, "log_tail": "",
               "pid": None, "created_at": _now(), "updated_at": _now()}
        with self._lock:
            jobs = self._read()
            jobs.append(job)
            self._write(jobs)
        return job

    def get(self, job_id: str) -> dict | None:
        return next((j for j in self._read() if j["job_id"] == job_id), None)

    def list(self, project_id: str | None = None,
             status: str | None = None) -> list[dict]:
        jobs = self._read()
        if project_id:
            jobs = [j for j in jobs if j["project_id"] == project_id]
        if status:
            jobs = [j for j in jobs if j["status"] == status]
        return sorted(jobs, key=lambda j: j["created_at"], reverse=True)

    def update(self, job_id: str, **fields) -> dict | None:
        with self._lock:
            jobs = self._read()
            for j in jobs:
                if j["job_id"] == job_id:
                    j.update(fields)
                    j["updated_at"] = _now()
                    self._write(jobs)
                    return j
        return None

    def cancel(self, job_id: str) -> dict | None:
        """Mark cancelled and SIGTERM the process group if running."""
        job = self.get(job_id)
        if job is None:
            return None
        if job.get("pid"):
            try:
                os.killpg(job["pid"], signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(job["pid"], signal.SIGTERM)
                except OSError:
                    pass
        if job["status"] in ("queued", "running"):
            return self.update(job_id, status="cancelled")
        return job


def run_render_job(registry: JobRegistry, job_id: str, cmd: list[str],
                   output: Path, total_seconds: float | None = None) -> dict:
    """Run an ffmpeg render command as a job, streaming -progress.

    The caller builds `cmd` with build_ffmpeg_command, then re-runs
    ffmpeg here with -progress added so we can report real progress.
    Progress = out_time_ms / total duration. If the total is unknown,
    progress stays 0 and the job still reports running/done/failed.
    """
    registry.update(job_id, status="running", progress=0.0)
    # Insert -progress before the output file (last arg).
    progress_file = Path(f"/tmp/veil-video-progress-{job_id}.txt")
    prog_cmd = cmd[:-1] + ["-progress", str(progress_file), cmd[-1]]
    log_lines: list[str] = []
    try:
        proc = subprocess.Popen(prog_cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        registry.update(job_id, pid=proc.pid)
        # Poll: read progress file, reap when done.
        while proc.poll() is None:
            time.sleep(0.5)
            if total_seconds and progress_file.exists():
                try:
                    txt = progress_file.read_text()
                    import re
                    m = re.findall(r"out_time_ms=(\d+)", txt)
                    if m:
                        done = int(m[-1]) / 1_000_000
                        pct = max(0.0, min(1.0, done / total_seconds))
                        registry.update(job_id, progress=round(pct, 3))
                except (OSError, ValueError):
                    pass
        out, _ = proc.communicate()
        if out:
            log_lines = out.splitlines()[-20:]
        if proc.returncode == 0 and Path(output).exists():
            registry.update(job_id, status="done", progress=1.0,
                            output=str(output),
                            log_tail="\n".join(log_lines))
        elif proc.returncode in (-15, -2):  # SIGTERM / SIGINT = cancelled
            registry.update(job_id, status="cancelled",
                            log_tail="\n".join(log_lines))
        else:
            registry.update(job_id, status="failed", progress=0.0,
                            error=f"ffmpeg exited {proc.returncode}",
                            log_tail="\n".join(log_lines))
    except Exception as e:
        registry.update(job_id, status="failed", error=f"{type(e).__name__}: {e}",
                        log_tail="\n".join(log_lines))
    finally:
        try:
            progress_file.unlink()
        except OSError:
            pass
    return registry.get(job_id) or {"job_id": job_id, "status": "failed",
                                    "error": "job vanished from registry"}


def run_function_job(registry: JobRegistry, job_id: str, fn, **kwargs) -> dict:
    """Run a plain Python function (transcribe, probe, scene detect)
    as a job. No fake progress: status goes queued -> running ->
    done/failed, and the function's return value is stored as the
    job output (JSON-serialisable)."""
    registry.update(job_id, status="running")
    try:
        result = fn(**kwargs)
        registry.update(job_id, status="done", progress=1.0,
                        output=result if isinstance(result, (dict, list, str)) else str(result))
    except Exception as e:
        registry.update(job_id, status="failed",
                        error=f"{type(e).__name__}: {e}")
    return registry.get(job_id) or {"job_id": job_id, "status": "failed"}


def start_thread(target, *args, **kwargs) -> threading.Thread:
    t = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t
