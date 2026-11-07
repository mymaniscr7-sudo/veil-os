# VEIL Video

**An AI-first video-editing subsystem for VEIL OS. Not a GUI editor
the AI has to click — a CLI/API the AI can actually use.**

The AI understands media through structured metadata, transcripts,
timestamps, scenes, and timeline JSON. It edits the timeline as
data; `render.py` turns that data into ffmpeg. It never scrubs a
playhead, never drags a clip, never composes ffmpeg arguments.

```
Windows Remote Client
        |  HTTPS (or HTTP over SSH tunnel / WireGuard in v1)
        v
VEIL OS Remote API  (remote_api.py, :8766)
        |
        v
VEIL Permission Broker  (every video op proposes first)
        |
        v
VEIL AI Runtime Container
        +-- VEIL Video CLI/API  (cli.py, video_tool.py)
        +-- FFmpeg  (renderer — installed, 8.1.2, verified live)
        +-- MLT / melt  (optional; NOT installed — .mlt export only)
        +-- faster-whisper  (optional; NOT installed — see below)
        +-- isolated project workspace  (projects/<id>/)
```

## Status (verified live 2026-11-06)

| Piece | State |
|---|---|
| `test_video.py` | **39/39 pass** — real ffmpeg, real in-process broker, real files |
| `demo.py` | end-to-end green: import → probe → timeline → scenes → render 6.0s 640x360 with audio → publish blocked without approval |
| FFmpeg / ffprobe 8.1.2 | installed, used for probe, scenes, contact sheets, proxies, render |
| faster-whisper | **not installed** — transcribe fails clearly with the install hint, never fakes a transcript |
| melt / MLT | **not installed** — ffmpeg is the v1 renderer; `.mlt` export works for Shotcut/Kdenlive |
| Remote API | smoke-tested live: health, state, submit task, project detail |
| Remote client | `remote_client.py` — stdlib-only, runs on Windows with plain Python 3 |

## Layout

```
veil-video/
  project.py        project model: manifest, timeline JSON, validation
  probe.py          ffprobe metadata, scene detection, contact sheets, proxies
  transcribe.py     faster-whisper wrapper (word timestamps, search, silences)
  render.py         timeline JSON -> deterministic ffmpeg filter graph
  jobs.py           render/transcribe/probe jobs: status, progress, cancel
  video_tool.py     broker-gated VeilVideo agent API (mirrors browser_tool.py)
  cli.py            `veil-video` CLI (--json for the AI runtime)
  remote_api.py     VEIL OS Remote API for the Windows client (:8766)
  remote_client.py  reference Windows client (stdlib only)
  test_video.py     39 tests, real media + real broker
  demo.py           end-to-end demo with synthetic media
  policy-video-editor.json   default policy for the video-editor env
  WINDOWS-CLIENT.md API contract + native-client notes
  projects/         isolated project workspaces (demo-cut lives here)
  tasks/            tasks submitted via the Remote API (queued for the AI)
```

## The AI's workflow

```
probe a-roll.mp4        -> {duration, 640x360, 30fps, has_audio, ...}
transcribe a-roll.mp4   -> transcript.json: words with timestamps
find "funniest"         -> [{word, start, end}, ...]
detect scenes           -> scenes.json: shot boundaries
timeline add-clip       -> {"clip_id":"c1","source":"a-roll.mp4","track":"v1",
                            "start":0,"in_point":12.4,"out_point":18.9}
render                  -> job-xxxxxxxx (poll: progress, done, output)
```

Edits reference *words and timestamps*, not pixels: "cut everything
between 'um' at 12.4s and the next sentence" is a timeline edit.

## Timeline model

`timeline.json`: `{version, width, height, fps, clips[]}`. A clip:

```json
{"clip_id": "c1", "source": "a-roll.mp4", "track": "v1",
 "start": 0, "in_point": 1.0, "out_point": 4.0,
 "speed": 1.0, "volume": 1.0,
 "transition_in": "fade", "transition_dur": 0.5}
```

Tracks: `v1` (main video), `v2` (picture-in-picture overlay),
`a1`/`a2` (extra audio — music, voiceover), `text` (title overlay).
v1 clips carry their own audio automatically. Validation rejects
out-of-range in/out points, v1 overlaps, bad tracks, and path
traversal in source names — before ffmpeg ever runs.

Render (v1): v1 clips trimmed to segment-local time, gaps filled
with black so timeline time is preserved, concatenated; v2
overlaid at its timeline position; audio trimmed, volume, delayed
to its start, mixed, padded to the exact timeline duration;
text via drawtext (skipped gracefully if no system font).
Output: H.264 + AAC, yuv420p, faststart, in `renders/`.

## Broker gating

| Capability | Action class | Behaviour |
|---|---|---|
| `video.read` | read | auto inside the project |
| `video.edit` | reversible-write | auto inside the project, snapshot/rollback |
| `video.render` | reversible-write | auto; output stays in `renders/` |
| `video.transcribe` | reversible-write | auto; transcript stays in the project |
| `video.export` / `video.publish` | external side effect | **user confirmation every time** — the file leaves the project |
| `video.delete` | irreversible/privileged (risk critical) | **explicit confirmation + step-up auth** |

Broker down → fail closed: nothing renders, nothing publishes.
Every proposal is journaled in `veil-core/audit-v2.log.jsonl`
(69 video.* entries from the test + demo runs). Media is copied
into `media/` on import — the AI never edits a host path in place.
`video_tool.py` mirrors `browser_tool.py`: service mode by
default, in-process mode only for tests/offline demo.

## Quick start

```bash
cd veil-os/veil-video
python3 test_video.py     # 39/39
python3 demo.py           # synthetic media, full pipeline

# CLI (in-process broker, for demo; service mode is the real path)
python3 cli.py --task demo --broker inprocess --policy policy-video-editor.json \
  --json create --name "Launch cut"
python3 cli.py --task demo --broker inprocess --policy policy-video-editor.json \
  --json import demo-cut /tmp/a-roll.mp4
python3 cli.py --task demo --broker inprocess --policy policy-video-editor.json \
  --json render demo-cut --wait

# Remote API (for the Windows client)
python3 remote_api.py --gen-token        # once; keep the token secret
VEIL_REMOTE_TOKEN=... python3 remote_api.py --port 8766
```

## Transcription: honest status

faster-whisper is **not installed** on the dev host (checked
2026-11-06), and neither is openai-whisper. `transcribe()` raises
`TranscribeUnavailable` with the install command instead of
inventing a transcript. To enable it (Arch target):

```
pacman -S python-faster-whisper   # or: pip install faster-whisper
```

First use downloads a model (`base` is the default) — that
download is a network action and goes through the broker. The
video-editor policy ships with network `offline` for this reason:
editing is local; turn the network on deliberately when you want
a model download.

## MLT / melt: honest status

melt is **not installed** on the dev host (checked 2026-11-06).
FFmpeg is the v1 renderer — it is installed, fast, and enough.
`export_mlt()` writes a `.mlt` XML from the same timeline for
Shotcut/Kdenlive interoperability; melt itself is only needed to
*play* that file, not to write it. If melt is installed later,
it becomes an alternate render path, not a requirement.

## Remote API

See `WINDOWS-CLIENT.md` for the full contract. In short:
`GET /api/video/state` (dashboard), `GET /api/video/projects[/:id]`,
`GET /api/video/jobs[/:id]`, `POST /api/video/tasks` (submit),
`POST /api/video/approvals/:id/approve|deny`,
`POST /api/video/tasks/:id/pause|resume|kill`. Bearer token auth;
v1 serves plain HTTP on 127.0.0.1 — put TLS termination, an SSH
tunnel, or WireGuard in front of it before exposing it to a
Windows machine over a network.
