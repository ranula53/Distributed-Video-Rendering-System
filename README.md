# Render-UI — Distributed Video Rendering System

A distributed system that speeds up video rendering by splitting a video into
chunks and rendering them **in parallel across multiple worker VMs**, then
stitching the results back into a single output file. It includes crash
recovery, worker fault tolerance, and an optional backup master for high
availability.

> **Note:** This README was generated from the project's internal
> `how-it-works.md` design notes. The master backend module (referred to here
> as `backend.py`) wasn't included in the files shared with me, so the exact
> CLI flags / environment variable names below are based on the documented
> design — double check them against the actual source before publishing.

---

## What it does

1. You upload **one video** through the web dashboard.
2. The **master** analyzes it (via `ffprobe`) and splits it into chunks with
   `ffmpeg`.
3. Each chunk is sent to a healthy **worker VM** over SSH/SCP, where it's
   rendered (scaled/re-encoded) in parallel.
4. Finished chunks are pulled back and merged into one `final_output.mp4`.
5. You download the finished video from the dashboard.

Because many machines render chunks simultaneously, jobs finish much faster
than a single machine re-encoding the whole video serially.

---

## Features

- **Parallel chunked rendering** — splits video into pieces and renders them
  concurrently across multiple worker VMs.
- **Automatic worker discovery** — uses Tailscale to find worker VMs on the
  private network without hardcoding IPs.
- **Health checks & retries** — pings workers before assigning work; failed
  or crashed renders are automatically reassigned to another healthy worker.
- **Crash recovery** — the master checkpoints job progress to
  `jobs/<job_id>/state.json`. If the master process/VM restarts, it resumes
  the job from where it left off instead of starting over.
- **Backup master / high availability (optional)** — a second master VM can
  take over automatically if the primary dies, using a shared NFS drive and
  a lease-based leader election.
- **Live progress dashboard** — a React + Tailwind UI that polls `/status`
  once a second to show worker health and an event timeline.
- **Automated tests** — crash-recovery and HA behavior are covered by Pytest
  (`tests/test_crash_recovery.py`, `tests/test_ha.py`).

---

## Architecture

| Component | Role | Location |
|---|---|---|
| **Master** | Splits video, distributes chunks, tracks progress, merges results | `backend.py` |
| **Workers** | Run `ffmpeg` on the chunk they're given, accessed over SSH — no custom code | Remote VMs |
| **Dashboard** | Displays progress/worker health; never renders anything itself | `src/App.jsx` |

The master is the only "smart" component — think of it as a head chef
splitting an order into dishes and handing each one to a different cook
(the workers), then plating everything back together at the end.

### Job folder layout (on the master)

```
uploads/                 original uploaded videos
master.lease             which master is primary (HA mode only)
jobs/<job_id>/
    chunk000.mp4 …       input pieces (from splitting)
    out_chunk000.mp4 …   rendered pieces (from workers)
    state.json           progress checkpoint (for crash recovery)
    final_output.mp4     the finished, merged video
```

---

## Tech stack

**Backend**
- Python + FastAPI — REST API (`/upload`, `/status`, `/health`, `/download`)
- Uvicorn — ASGI server that runs the FastAPI app
- ffmpeg / ffprobe — splitting, scaling/re-encoding, merging, and inspecting videos
- SSH / SCP — running remote commands and moving chunks to/from workers
- Tailscale — private network + auto-discovery of worker VMs
- Python `threading` — parallel health checks with a lock-protected shared status
- NFS + lease-based leader election — shared state between primary/backup masters (HA mode)
- systemd — auto-start/restart of the master service
- Pytest — automated tests

**Frontend**
- React — dashboard UI (`src/App.jsx`)
- Vite — dev server / production bundler
- Tailwind CSS — styling

---

## API endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/upload` | `POST` | Upload a video and start a render job |
| `/status` | `GET` | Current job state (workers, progress, event log) — polled every second by the dashboard |
| `/health` | `GET` | Ping all workers on demand |
| `/download/<job_id>` | `GET` | Download the finished `final_output.mp4` |

---

## Getting started

### Prerequisites

- Python 3.10+
- Node.js 18+ and npm
- `ffmpeg` and `ffprobe` installed and on `PATH`
- Worker VMs reachable over SSH, joined to a shared [Tailscale](https://tailscale.com) network
- SSH key-based access from the master to each worker (no password prompts)

### 1. Backend (master)

```bash
# from the project root, in a virtual environment
pip install fastapi uvicorn

# start the master API
uvicorn backend:app --host 0.0.0.0 --port 8000
```

The master will read the list of available workers from `tailscale status`
at runtime, so make sure Tailscale is running and connected before starting.

### 2. Frontend (dashboard)

```bash
cd src/.. # project root containing package.json
npm install

# development
npm run dev

# production build
npm run build
```

Point the dashboard's API base URL at the master (e.g. `http://<master-tailscale-ip>:8000`).

### 3. Run a job

1. Open the dashboard in your browser.
2. Upload a video file.
3. Watch the event timeline and worker status update live.
4. Click **Download Video** once the job reaches 100%.

### 4. Running tests

```bash
pytest tests/
```

### 5. Optional: enable the backup master (HA)

1. Mount the same NFS share for `uploads/`, `jobs/`, and `master.lease` on
   both master VMs.
2. Set `HA_ENABLED=1` on both masters before starting them.
3. See `docs/backup-master-setup.md` for full setup steps.

With `HA_ENABLED` unset, the system behaves as a single master with no
change in behavior.

---

## Fault tolerance summary

- A failed chunk render is retried and reassigned to another worker.
- If no workers are free, the master re-checks health before giving up.
- If the master process/VM restarts, it resumes the job from
  `state.json`, only re-rendering chunks that don't already have a
  finished `out_*.mp4` on disk.
- If the primary master dies entirely (HA mode), the standby master takes
  over via the shared lease file and resumes the job.

The one thing this system deliberately does **not** do is keep redundant
copies of chunks (replication) — resilience comes from re-rendering lost
work, not from replicas.

---

## Distributed systems transparency

"Transparency" here means hiding the fact that many separate computers are
involved, so the whole thing feels like one system. This project demonstrates
several of the classic transparency types:

- **Access transparency** — Every worker is driven the exact same way
  (identical SSH commands), and you only ever touch one simple web interface
  (upload/download). You never issue SSH, SCP, or ffmpeg yourself.
- **Location transparency** — Workers are referred to by name ("Worker 2"),
  never by IP. Tailscale discovers them at runtime, so where they physically
  live doesn't matter.
- **Concurrency transparency** — Many workers render different chunks at the
  same time, and a lock keeps the shared status consistent — yet you just
  watch one coherent progress bar, unaware of the parallelism underneath.
- **Failure transparency** — Worker crashes are retried and reassigned, a
  master restart auto-resumes the job, and a dead master fails over to the
  backup — all so you still get one complete video. Failures are recovered
  *for* you (though still *shown* in the event timeline on purpose, as a
  deliberate trade-off for observability).
- **Scaling / performance transparency** — The system adapts to load on its
  own: it picks the number of chunks from the video's size and how many
  workers are online, and adding another worker needs no code change.
- **Migration transparency (partial)** — A chunk that fails on one worker is
  quietly moved to another; you never notice the move.

The one type this system deliberately does **not** provide is **replication
transparency** — chunks aren't kept as multiple copies; resilience comes from
*re-rendering* a lost chunk, not from replicas.

---

## Known limitations

- The HA lease is a shared file on NFS, not a true atomic distributed lock —
  adequate for this project, not production-grade. Tools like etcd,
  ZooKeeper, or Redis would give stronger guarantees in a production system.
- If the NFS share itself lives on one of the master VMs, that VM becomes a
  new single point of failure — for full resilience it should live on a
  separate machine.