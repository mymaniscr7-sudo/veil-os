# Windows Remote Client — API contract

The Windows client talks to VEIL OS through one endpoint: the
Remote API (`remote_api.py`, default `http://127.0.0.1:8766` on the
VEIL machine). It never talks to the broker, the AI runtime, or
ffmpeg directly.

## Connecting

v1 serves plain HTTP on loopback. From a Windows machine, reach
it one of these ways (in order of preference):

1. **SSH tunnel** — `ssh -L 8766:127.0.0.1:8766 user@veil-host`,
   then the client points at `http://127.0.0.1:8766`.
2. **WireGuard / Tailscale** — the VEIL host joins the user's
   private network; the client points at the VEIL host's VPN IP.
3. **TLS reverse proxy** — Caddy/nginx in front of the API with a
   real certificate. (Later; not built in v1.)

Auth is a Bearer token on every call except `GET /api/health`.
Get the token once on the VEIL host:

```bash
python3 remote_api.py --gen-token   # saves run/remote-token (0600)
```

The token is the Windows client's login. Rotate it by deleting
`run/remote-token` and generating a new one. The reference client
reads it from `VEIL_REMOTE_TOKEN`.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health` | liveness, no auth, no data |
| GET | `/api/video/state` | dashboard: broker, task, pending approvals, projects, jobs, tasks |
| GET | `/api/video/projects` | all projects (manifest + clip count + duration + renders) |
| GET | `/api/video/projects/:id` | manifest + timeline + transcript + scenes + jobs |
| GET | `/api/video/jobs?project=:id` | jobs, newest first |
| GET | `/api/video/jobs/:id` | one job: status, progress, output, error, log tail |
| GET | `/api/video/tasks` | submitted tasks |
| POST | `/api/video/tasks` | submit a task: `{"project_id"?,"instruction"}` → queued for the AI |
| POST | `/api/video/approvals/:id/approve` | approve a pending dangerous action (via broker admin socket) |
| POST | `/api/video/approvals/:id/deny` | deny it |
| POST | `/api/video/tasks/:id/pause` | SIGSTOP the AI task process |
| POST | `/api/video/tasks/:id/resume` | SIGCONT |
| POST | `/api/video/tasks/:id/kill` | SIGTERM + clear its pending approvals |

All responses are JSON. Errors are `{"error": "..."}` with a
4xx/5xx status. A `401` means the Bearer token is missing/wrong.

## What the client shows

- **Activity:** the current AI task (from `/api/video/state`),
  its step, and whether it is paused.
- **Video projects:** project list with clip counts, durations,
  render counts; drill into a project for its timeline, transcript
  (words with timestamps), scenes, and jobs.
- **Jobs:** render / transcribe / scene jobs with real progress
  (renders report `progress` 0..1 from ffmpeg; transcribe/scene
  jobs report running/done/failed honestly, no fake percentage).
- **Approvals:** pending items show the *exact* action, target,
  and expected effect — e.g. `video.publish render.mp4 ->
  /home/user/Videos/out.mp4`. Approve/deny from the client; the
  decision goes through the broker, exactly like the Security
  Center. Approving a publish lets that one exact copy happen.
- **Task control:** pause, resume, kill. Killing also clears the
  task's pending approvals so a dead task can't be approved
  after the fact.

## What the client cannot do

- Execute a video operation. Submitting a task only queues it;
  the AI runtime performs the work through VeilVideo, proposing
  every step to the broker. A compromised client can queue tasks
  and approve items the user sees — it cannot make the broker
  allow something unapproved, and it cannot read outside the
  video projects.
- Bypass the broker. There is no endpoint that renders, publishes,
  or deletes. Those are AI-runtime operations, broker-gated.

## Reference client

`remote_client.py` is stdlib-only Python 3 and runs on Windows
as-is (`python remote_client.py state`). It exercises every
endpoint above. A native Windows client (WinUI / Electron /
Tauri) calls the same endpoints; this contract is the spec.

```powershell
$env:VEIL_REMOTE_URL="http://127.0.0.1:8766"
$env:VEIL_REMOTE_TOKEN="<token>"
python remote_client.py state
python remote_client.py submit --instruction "Cut a 60s trailer from a-roll.mp4, keep the funniest lines"
python remote_client.py watch job-xxxxxxxx
python remote_client.py approve prop-xxxxxxxx
python remote_client.py pause
```

## Polling

Poll `/api/video/state` every 2–5s for the dashboard, or
`/api/video/jobs/:id` for a single job. There is no WebSocket
in v1 — the architecture diagram shows HTTPS *or* WebSocket;
v1 ships HTTPS (HTTP on loopback) + polling, and a WebSocket
push channel for job progress is the next step, not a v1
requirement. Do not poll faster than 1s; the state endpoint
reads the broker, the project dirs, and the job registries on
every call.
