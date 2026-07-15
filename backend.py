from fastapi import FastAPI, UploadFile, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import shlex
import uuid
import glob
import os
import socket
import time
import threading
from collections import deque
from datetime import datetime, timezone


import json


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Configuration & Worker Discovery
# ---------------------------------------------------------------------------
FALLBACK_WORKERS = [
    {"id": "worker_1", "address": "vm2@100.81.142.36", "name": "Worker 1"},
    {"id": "worker_2", "address": "vm3@100.126.109.8",  "name": "Worker 2"},
    {"id": "worker_3", "address": "vm4@100.110.176.56", "name": "Worker 3"},
]

TARGET_RES = "1280:720"
MAX_RETRIES_PER_CHUNK = 2
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=2"
]
HEALTH_CHECK_TIMEOUT = 5    # seconds

# ---------------------------------------------------------------------------
# High-availability (active/standby master via a shared NFS lease)
# ---------------------------------------------------------------------------
NODE_ID = os.environ.get("NODE_ID", socket.gethostname())
HA_ENABLED = os.environ.get("HA_ENABLED", "0") == "1"
LEASE_FILE = os.path.abspath("master.lease")
LEASE_TTL = 10.0        # seconds without an epoch change before a standby takes over
LEASE_RENEW = 2.0       # seconds between supervisor iterations

role = "primary"        # "primary" | "standby" (default suits single-master / HA-disabled)
primary_node = NODE_ID

# Hostnames that are MASTERS (not workers) — excluded from worker discovery so a
# second/standby master is never mistaken for a worker. Comma-separated env var,
# e.g. MASTER_HOSTS=vm1,vm10
MASTER_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("MASTER_HOSTS", "").split(",")
    if h.strip()
}

def discover_tailscale_workers() -> list:
    """
    Run 'tailscale status --json' to discover worker nodes dynamically.
    Filters machines whose hostname starts with 'vm' and isn't the master.
    Falls back to FALLBACK_WORKERS if tailscale is not found or fails.
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        data = json.loads(result.stdout)
        master_hostname = data.get("Self", {}).get("HostName", "vm1")
        
        peers = data.get("Peer", {})
        discovered = []
        idx = 1
        
        sorted_peers = sorted(peers.items(), key=lambda x: x[1].get("HostName", ""))
        for _, info in sorted_peers:
            hostname = info.get("HostName", "")
            ips = info.get("TailscaleIPs", [])
            if not ips:
                continue
            ip = ips[0]
            if (hostname.lower().startswith("vm")
                    and hostname.lower() != master_hostname.lower()
                    and hostname.lower() not in MASTER_HOSTS):
                discovered.append({
                    "id": f"worker_{idx}",
                    "address": f"{hostname}@{ip}",
                    "name": f"Worker {idx} ({hostname})"
                })
                idx += 1

        if discovered:
            return discovered
    except Exception:
        pass
    return FALLBACK_WORKERS

WORKERS = discover_tailscale_workers()

# ---------------------------------------------------------------------------
# System status  (the single source of truth consumed by the frontend)
# ---------------------------------------------------------------------------
def _make_initial_status():
    return {
        "state": "Idle",
        "job_id": None,
        "master": "Waiting for video...",
        "total_chunks": 0,
        "completed_chunks": 0,
        "workers": {
            w["id"]: {
                "address": w["address"],
                "name": w["name"],
                "status": "Idle",
                "health": "unknown",
                "chunk": None,
                "progress": "",
            }
            for w in WORKERS
        },
        "event_log": [],
    }

system_status = _make_initial_status()
_status_lock = threading.Lock()


def _log_event(msg: str, etype: str = "info"):
    """Append a timestamped event to the event log (thread-safe)."""
    with _status_lock:
        system_status["event_log"].append({
            "time": datetime.now(timezone.utc).isoformat(),
            "type": etype,      # info | warn | error | success
            "msg": msg,
        })
        # Keep the last 50 events to avoid unbounded growth
        if len(system_status["event_log"]) > 50:
            system_status["event_log"] = system_status["event_log"][-50:]


def _set_worker(wid: str, **kwargs):
    with _status_lock:
        for k, v in kwargs.items():
            system_status["workers"][wid][k] = v


def _set_master(**kwargs):
    with _status_lock:
        for k, v in kwargs.items():
            system_status[k] = v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def run_cmd(cmd_list, timeout=None):
    """Run a command safely as an argv list."""
    subprocess.run(cmd_list, check=True, timeout=timeout)


def _lease_tmp_path():
    """Node-unique temp path for the lease write. Both masters share the NFS
    directory, so the temp file must not collide across nodes."""
    return f"{LEASE_FILE}.{NODE_ID}.tmp"


def _write_lease(holder: str, epoch: int):
    """Atomically write the HA lease file (node-unique temp + os.replace)."""
    tmp = _lease_tmp_path()
    with open(tmp, "w") as f:
        json.dump({"holder": holder, "epoch": epoch}, f)
    os.replace(tmp, LEASE_FILE)


def _read_lease():
    """Return the lease dict {holder, epoch}, or None if missing/corrupt."""
    try:
        with open(LEASE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _lease_decision(lease, node_id, seen_epoch, last_change, now, ttl):
    """Pure decision for the lease loop. `now` is a monotonic timestamp.
    Returns (action, new_seen_epoch, new_last_change);
    action in {'acquire','renew','standby','takeover'}."""
    if lease is None:
        return ("acquire", None, now)
    if lease.get("holder") == node_id:
        return ("renew", lease.get("epoch"), now)
    epoch = lease.get("epoch")
    if epoch != seen_epoch:
        seen_epoch = epoch
        last_change = now
    if now - last_change >= ttl:
        return ("takeover", seen_epoch, last_change)
    return ("standby", seen_epoch, last_change)


def _fetch_output(address: str, remote_out: str, local_out: str):
    """SCP a rendered chunk back from a worker atomically: download to a
    temporary '.part' file and os.replace() it into place, so a crash during
    transfer never leaves a truncated output file that resume would trust as
    complete."""
    local_tmp = local_out + ".part"
    run_cmd(["scp"] + SSH_OPTS + [f"{address}:{remote_out}", local_tmp])
    os.replace(local_tmp, local_out)


def check_worker_alive(address: str) -> bool:
    """SSH-ping a worker. Returns True if it responds within HEALTH_CHECK_TIMEOUT."""
    try:
        subprocess.run(
            ["ssh"] + SSH_OPTS + [address, "echo", "ok"],
            check=True,
            timeout=HEALTH_CHECK_TIMEOUT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def probe_all_workers(log_events: bool = True):
    """Check every worker in parallel and update system_status.health.

    When ``log_events`` is False the probe still updates each worker's health
    and status, but does not append entries to the event timeline. This is used
    by the on-demand /health endpoint so manual health checks don't clutter the
    timeline (their result is shown in a popup on the frontend instead).
    """
    threads = []
    results = {}

    def _probe(w):
        alive = check_worker_alive(w["address"])
        results[w["id"]] = alive

    for w in WORKERS:
        t = threading.Thread(target=_probe, args=(w,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    for w in WORKERS:
        wid = w["id"]
        health = "online" if results.get(wid) else "offline"
        _set_worker(wid, health=health)
        if health == "offline":
            _set_worker(wid, status="Offline")
            if log_events:
                _log_event(f"{w['name']} ({w['address']}) is OFFLINE", "warn")
        else:
            if system_status["workers"][wid]["status"] in ("Idle", "Offline"):
                _set_worker(wid, status="Online")
            if log_events:
                _log_event(f"{w['name']} ({w['address']}) is online", "info")

    return results


def get_alive_workers():
    """Return list of WORKER dicts whose health == 'online'."""
    return [
        w for w in WORKERS
        if system_status["workers"][w["id"]]["health"] == "online"
    ]


def calculate_segments(file_path: str, num_workers: int):
    # 1. Get file size in MB
    file_size_bytes = os.path.getsize(file_path)
    file_size_mb = file_size_bytes / (1024 * 1024)
    
    # 2. Get video info via ffprobe
    duration = 30.0  # default fallback
    width = 1920
    height = 1080
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "default=noprint_wrappers=1",
            file_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        for line in res.stdout.strip().split("\n"):
            if "=" in line:
                key, val = line.split("=", 1)
                if key == "width":
                    width = int(val)
                elif key == "height":
                    height = int(val)
                elif key == "duration":
                    duration = float(val)
    except Exception:
        pass

    # 3. Determine dynamic target resolution to preserve aspect orientation
    try:
        tw, th = map(int, TARGET_RES.split(":"))
    except Exception:
        tw, th = 1280, 720
    
    large_dim = max(tw, th)
    small_dim = min(tw, th)
    
    if height > width:
        # Input is portrait
        target_res = f"{small_dim}:{large_dim}"
    elif width > height:
        # Input is landscape
        target_res = f"{large_dim}:{small_dim}"
    else:
        # Input is square
        target_res = f"{small_dim}:{small_dim}"
        
    # 4. Decide target chunk count based on file size and active workers
    if file_size_mb < 5.0:
        target_chunks = max(3, num_workers)
    elif file_size_mb < 20.0:
        target_chunks = max(6, num_workers * 2)
    elif file_size_mb < 100.0:
        target_chunks = max(12, num_workers * 4)
    else:
        target_chunks = max(24, num_workers * 8)
        
    # 5. Calculate segment time
    segment_time = duration / target_chunks
    # Clamp segment time to at least 1.0 second to prevent ffmpeg from creating empty segments
    if segment_time < 1.0:
        segment_time = 1.0
        target_chunks = int(duration / segment_time)
        if target_chunks < 1:
            target_chunks = 1
            
    return target_chunks, f"{segment_time:.2f}", file_size_mb, duration, target_res


# ---------------------------------------------------------------------------
# Durable job checkpoint (crash-recovery)
# ---------------------------------------------------------------------------
TERMINAL_PHASES = ("completed", "error")

_job_state = None            # dict: durable checkpoint content for the active job, or None
_job_lock = threading.Lock()


def _checkpoint_file(job_id: str) -> str:
    return os.path.join(os.path.abspath(os.path.join("jobs", job_id)), "state.json")


def _write_checkpoint(job_id: str, state: dict):
    """Atomically write the job checkpoint to jobs/<job_id>/state.json."""
    path = _checkpoint_file(job_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = dict(state)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)      # atomic on the same filesystem


def _read_checkpoint(job_id: str):
    """Return the checkpoint dict for job_id, or None if missing/corrupt."""
    try:
        with open(_checkpoint_file(job_id)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _init_job_state(job_id: str, file_path: str, base: dict = None):
    """Initialise the in-memory durable job state (fresh, or seeded from a checkpoint)
    and immediately persist it so this job supersedes older ones by updated_at."""
    global _job_state
    state = {
        "job_id": job_id,
        "file_path": file_path,
        "phase": "splitting",
        "target_res": None,
        "segment_time": None,
        "total_chunks": 0,
        "retries": {},
        "failed_permanently": [],
    }
    if base:
        for k in state:
            if k in base:
                state[k] = base[k]
        state["job_id"] = job_id
        state["file_path"] = file_path or base.get("file_path", "")
    with _job_lock:
        _job_state = state
        _write_checkpoint(job_id, dict(state))


def _checkpoint(**changes):
    """Merge changes into the active durable job state and atomically persist it.
    No-op if there is no active job."""
    with _job_lock:
        if _job_state is None:
            return
        _job_state.update(changes)
        _write_checkpoint(_job_state["job_id"], dict(_job_state))


def _output_exists(job_dir: str, chunk_name: str) -> bool:
    out = os.path.join(job_dir, f"out_{chunk_name}")
    return os.path.isfile(out) and os.path.getsize(out) > 0


def _compute_pending(job_dir: str, chunks: list, failed_permanently) -> list:
    """Chunk indices still needing a render: output missing/empty and not permanently failed.
    Disk is the source of truth for completion."""
    failed = set(failed_permanently)
    pending = []
    for i, chunk_path in enumerate(chunks):
        if i in failed:
            continue
        if not _output_exists(job_dir, os.path.basename(chunk_path)):
            pending.append(i)
    return pending


def _find_resumable_job(jobs_root: str = "jobs"):
    """Return (job_id, checkpoint) for the most-recently-updated job if it is
    non-terminal, else None. 'Newest wins', so a fresh upload supersedes older jobs."""
    root = os.path.abspath(jobs_root)
    if not os.path.isdir(root):
        return None
    latest = None   # (updated_at, job_id, ckpt)
    for job_id in os.listdir(root):
        ckpt = _read_checkpoint(job_id)
        if not ckpt:
            continue
        ts = ckpt.get("updated_at", "")
        if latest is None or ts > latest[0]:
            latest = (ts, job_id, ckpt)
    if latest and latest[2].get("phase") not in TERMINAL_PHASES:
        return latest[1], latest[2]
    return None


# ---------------------------------------------------------------------------
# Core rendering logic  (fault-tolerant, work-queue based)
# ---------------------------------------------------------------------------
def process_video_task(file_path: str, job_id: str, resume: bool = False):
    global system_status, WORKERS
    WORKERS = discover_tailscale_workers()

    job_dir = os.path.abspath(f"jobs/{job_id}")
    os.makedirs(job_dir, exist_ok=True)

    ckpt = _read_checkpoint(job_id) if resume else None
    _init_job_state(job_id, file_path, ckpt)

    if resume:
        _set_master(state="Resuming", job_id=job_id,
                    master="Resuming previous job after restart...")
        _log_event(f"Resuming job {job_id[:8]}… after master restart", "warn")
    else:
        _set_master(state="Processing", job_id=job_id, master="Initialising job...")
        _log_event(f"Job {job_id[:8]}… started", "info")

    try:
        # ── 1. Health check ──────────────────────────────────────────────
        _set_master(master="Running health checks on workers...")
        _log_event("Probing all workers...", "info")
        probe_all_workers()

        alive = get_alive_workers()
        if not alive:
            raise RuntimeError("No workers are online. Cannot proceed.")
        _log_event(f"{len(alive)} worker(s) available", "info")

        # ── 2. Split video (skip if resuming with chunks already on disk) ─
        existing_chunks = sorted(glob.glob(os.path.join(job_dir, "chunk*.mp4")))
        if resume and existing_chunks:
            chunks = existing_chunks
            target_res = (ckpt or {}).get("target_res") or TARGET_RES
            _log_event(f"Reusing {len(chunks)} existing chunk(s) from disk", "info")
        else:
            if resume and not os.path.isfile(file_path):
                raise RuntimeError("Cannot resume: source video and chunks are both missing.")
            _set_master(master="Analyzing video size and duration...")
            num_workers = len(alive)
            target_chunks, segment_time_str, file_size_mb, duration, target_res = calculate_segments(file_path, num_workers)

            _log_event(
                f"Video Analysis: Size {file_size_mb:.2f} MB, Duration {duration:.2f}s, Target Resolution {target_res}. "
                f"Splitting into {target_chunks} chunks (segment time: {segment_time_str}s) "
                f"for {num_workers} active worker(s).",
                "info"
            )

            _set_master(master="Splitting video into chunks...")
            segment_pattern = os.path.join(job_dir, "chunk%03d.mp4")
            run_cmd([
                "ffmpeg", "-y", "-i", file_path,
                "-map", "0:v:0", "-map", "0:a:0",
                "-c", "copy",
                "-segment_time", segment_time_str,
                "-f", "segment",
                segment_pattern,
            ])

            chunks = sorted(glob.glob(os.path.join(job_dir, "chunk*.mp4")))
            if not chunks:
                raise RuntimeError("ffmpeg produced zero chunks.")
            _checkpoint(phase="rendering", total_chunks=len(chunks),
                        segment_time=segment_time_str, target_res=target_res)
            _log_event(f"Video split into {len(chunks)} chunk(s)", "success")

        _set_master(total_chunks=len(chunks))

        # ── 3. Work-queue setup (unified fresh/resume; completed derived from disk) ─
        _set_master(master="Distributing chunks to workers...")

        failed_permanently = list((ckpt or {}).get("failed_permanently") or [])
        retries = {int(k): v for k, v in ((ckpt or {}).get("retries") or {}).items()}
        for i in range(len(chunks)):
            retries.setdefault(i, 0)
        pending = deque(_compute_pending(job_dir, chunks, failed_permanently))
        completed_chunks_set = set(range(len(chunks))) - set(pending) - set(failed_permanently)
        active = {}    # wid -> (Popen, chunk_idx, chunk_name, remote_out)

        _set_master(completed_chunks=len(completed_chunks_set))
        _checkpoint(phase="rendering", total_chunks=len(chunks), target_res=target_res,
                    retries=retries, failed_permanently=failed_permanently)
        if resume and completed_chunks_set:
            _log_event(
                f"{len(completed_chunks_set)} chunk(s) already rendered; {len(pending)} remaining",
                "info",
            )

        def _persist_progress():
            _checkpoint(retries=retries, failed_permanently=failed_permanently)

        def _assign_chunk(wid, chunk_idx):
            """Send a chunk to a worker and start the remote render."""
            w = next(w for w in WORKERS if w["id"] == wid)
            chunk_path = chunks[chunk_idx]
            chunk_name = os.path.basename(chunk_path)
            out_name = f"out_{chunk_name}"
            remote_dir = f"/tmp/{job_id}"
            remote_in = f"{remote_dir}/{chunk_name}"
            remote_out = f"{remote_dir}/{out_name}"

            _set_worker(wid, status="Receiving", chunk=chunk_name,
                        progress=f"Receiving {chunk_name}...")
            _log_event(f"{w['name']} ← sending {chunk_name}", "info")

            # mkdir + scp
            run_cmd(["ssh"] + SSH_OPTS + [w["address"], "mkdir", "-p", remote_dir])
            run_cmd(["scp"] + SSH_OPTS + [chunk_path, f"{w['address']}:{remote_in}"])

            # start remote ffmpeg
            _set_worker(wid, status="Rendering",
                        progress=f"Rendering {chunk_name} to {target_res}...")
            _log_event(f"{w['name']} rendering {chunk_name}...", "info")

            remote_cmd = (
                f"ffmpeg -y -i {shlex.quote(remote_in)} "
                f"-vf scale={target_res} {shlex.quote(remote_out)}"
            )
            p = subprocess.Popen(
                ["ssh"] + SSH_OPTS + [w["address"], remote_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            active[wid] = (p, chunk_idx, chunk_name, remote_out)

        def _collect_result(wid):
            """SCP the rendered chunk back from the worker."""
            w = next(w for w in WORKERS if w["id"] == wid)
            _, chunk_idx, chunk_name, remote_out = active[wid]
            out_name = f"out_{chunk_name}"
            local_out = os.path.join(job_dir, out_name)

            _set_worker(wid, status="Transferring",
                        progress=f"Downloading {out_name}...")
            _log_event(f"{w['name']} → fetching {out_name}", "info")

            _fetch_output(w["address"], remote_out, local_out)

            _set_worker(wid, status="Completed", progress="Done")
            _log_event(f"{w['name']} completed {chunk_name}", "success")
            completed_chunks_set.add(chunk_idx)
            _set_master(completed_chunks=len(completed_chunks_set))
            _persist_progress()

        # Seed: assign first batch of chunks to alive workers
        alive_ids = [w["id"] for w in get_alive_workers()]
        for wid in alive_ids:
            if pending:
                cidx = pending.popleft()
                try:
                    _assign_chunk(wid, cidx)
                except Exception as exc:
                    _log_event(f"Failed to send chunk to {wid}: {exc}", "error")
                    _set_worker(wid, status="Failed", health="offline",
                                progress=str(exc))
                    retries[cidx] += 1
                    if retries[cidx] <= MAX_RETRIES_PER_CHUNK:
                        pending.append(cidx)

        # Poll loop
        while active or pending:
            finished_wids = []
            for wid, (p, cidx, cname, rout) in list(active.items()):
                ret = p.poll()
                if ret is None:
                    continue  # still running
                if ret == 0:
                    # Success — collect result
                    try:
                        _collect_result(wid)
                    except Exception as exc:
                        _log_event(f"Transfer back from {wid} failed: {exc}", "error")
                        _set_worker(wid, status="Failed", progress=str(exc))
                        retries[cidx] += 1
                        if retries[cidx] <= MAX_RETRIES_PER_CHUNK:
                            pending.append(cidx)
                            _log_event(f"Re-queuing {cname} (retry {retries[cidx]})", "warn")
                        else:
                            failed_permanently.append(cidx)
                            _log_event(f"{cname} failed permanently after {MAX_RETRIES_PER_CHUNK} retries", "error")
                    finished_wids.append(wid)
                else:
                    # Render failed on this worker
                    w = next(w for w in WORKERS if w["id"] == wid)
                    _set_worker(wid, status="Failed", health="offline",
                                progress=f"Render exited with code {ret}")
                    _log_event(
                        f"{w['name']} FAILED on {cname} (exit {ret}) - reassigning...",
                        "error",
                    )
                    retries[cidx] += 1
                    if retries[cidx] <= MAX_RETRIES_PER_CHUNK:
                        pending.append(cidx)
                        _log_event(f"Re-queuing {cname} (retry {retries[cidx]})", "warn")
                    else:
                        failed_permanently.append(cidx)
                        _log_event(f"{cname} failed permanently after {MAX_RETRIES_PER_CHUNK} retries", "error")
                    finished_wids.append(wid)

            for wid in finished_wids:
                del active[wid]

            # Assign pending chunks to free, alive workers
            if pending:
                free_alive = [
                    w["id"] for w in WORKERS
                    if w["id"] not in active
                    and system_status["workers"][w["id"]]["health"] == "online"
                ]
                for wid in free_alive:
                    if not pending:
                        break
                    cidx = pending.popleft()
                    cname = os.path.basename(chunks[cidx])
                    _set_worker(wid, status="Reassigned",
                                progress=f"Picking up {cname} (retry {retries[cidx]})...")
                    _log_event(
                        f"Reassigning {cname} → {system_status['workers'][wid]['name']}",
                        "warn",
                    )
                    _set_master(master=f"Reassigning {cname}...")
                    try:
                        _assign_chunk(wid, cidx)
                    except Exception as exc:
                        _log_event(f"Reassignment to {wid} failed: {exc}", "error")
                        _set_worker(wid, status="Failed", health="offline",
                                    progress=str(exc))
                        retries[cidx] += 1
                        if retries[cidx] <= MAX_RETRIES_PER_CHUNK:
                            pending.append(cidx)
                        else:
                            failed_permanently.append(cidx)

                # If there are still pending chunks but no alive workers, do a fresh probe
                if pending and not free_alive:
                    _log_event("No free workers - re-probing health...", "warn")
                    _set_master(master="Re-checking worker availability...")
                    probe_all_workers()
                    new_alive = [
                        w["id"] for w in get_alive_workers()
                        if w["id"] not in active
                    ]
                    if not new_alive and not active:
                        raise RuntimeError(
                            "All workers offline with chunks still pending. "
                            f"Pending: {[os.path.basename(chunks[i]) for i in pending]}"
                        )

            _persist_progress()
            time.sleep(0.3)   # avoid busy-wait

        if failed_permanently:
            names = [os.path.basename(chunks[i]) for i in failed_permanently]
            raise RuntimeError(f"Chunks failed permanently: {', '.join(names)}")

        # ── 4. Merge ────────────────────────────────────────────────────
        _set_master(master="Merging rendered chunks...")
        _checkpoint(phase="merging")
        _log_event("Merging all rendered chunks...", "info")

        list_path = os.path.join(job_dir, "list.txt")
        with open(list_path, "w") as f:
            for i in range(len(chunks)):
                out_name = f"out_{os.path.basename(chunks[i])}"
                f.write(f"file '{os.path.join(job_dir, out_name)}'\n")

        final_output = os.path.join(job_dir, "final_output.mp4")
        run_cmd([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", list_path, "-c", "copy", final_output,
        ])

        _set_master(master="Rendering complete!", state="Completed")
        _checkpoint(phase="completed")
        _log_event("Job finished successfully!", "success")

    except Exception as e:
        _set_master(state="Error", master=str(e))
        _checkpoint(phase="error")
        _log_event(f"FATAL: {e}", "error")


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.post("/upload")
async def upload_video(file: UploadFile, background_tasks: BackgroundTasks):
    global system_status, WORKERS
    if HA_ENABLED and role != "primary":
        raise HTTPException(status_code=409,
                            detail=f"This node is standby; primary is {primary_node}")
    WORKERS = discover_tailscale_workers()
    # Reset status for new job
    system_status = _make_initial_status()

    job_id = uuid.uuid4().hex
    os.makedirs("uploads", exist_ok=True)

    ext = os.path.splitext(file.filename or "")[1] or ".mp4"
    file_path = os.path.join("uploads", f"{job_id}{ext}")

    with open(file_path, "wb") as buffer:
        buffer.write(await file.read())

    background_tasks.add_task(process_video_task, file_path, job_id)
    return {"message": "Upload successful, rendering started.", "job_id": job_id}


_resume_thread = None


def _resume_if_needed():
    """On startup or promotion, auto-resume the most recent unfinished job.
    No-op if a resume this node started is still running (prevents a flapping
    lease from double-spawning a render for the same job)."""
    global system_status, _resume_thread
    if _resume_thread is not None and _resume_thread.is_alive():
        return
    found = _find_resumable_job()
    if not found:
        return
    job_id, ckpt = found
    file_path = ckpt.get("file_path", "")
    system_status = _make_initial_status()
    _log_event(f"Found incomplete job {job_id[:8]}…; auto-resuming after restart", "warn")
    _resume_thread = threading.Thread(
        target=process_video_task,
        args=(file_path, job_id),
        kwargs={"resume": True},
        daemon=True,
    )
    _resume_thread.start()


def _become_primary():
    """Transition this node to primary; resume any interrupted job once, on promotion."""
    global role, primary_node
    was_standby = role != "primary"
    role, primary_node = "primary", NODE_ID
    if was_standby:
        _log_event(f"{NODE_ID} promoted to PRIMARY", "warn")
        _resume_if_needed()


def _become_standby(holder):
    global role, primary_node
    role, primary_node = "standby", holder


def _lease_supervisor():
    """Leader-election loop: renew the lease if primary, take over if the primary's
    lease goes stale. Runs only when HA_ENABLED."""
    seen_epoch, last_change = None, time.monotonic()
    while True:
        lease = _read_lease()
        action, seen_epoch, last_change = _lease_decision(
            lease, NODE_ID, seen_epoch, last_change, time.monotonic(), LEASE_TTL)
        if action in ("acquire", "renew", "takeover"):
            new_epoch = 1 if lease is None else lease.get("epoch", 0) + 1
            _write_lease(NODE_ID, new_epoch)
            confirm = _read_lease()                    # re-read to resolve simultaneous claims
            if confirm and confirm.get("holder") == NODE_ID:
                _become_primary()
            else:
                _become_standby(confirm.get("holder") if confirm else None)
        else:
            _become_standby(lease.get("holder"))
        time.sleep(LEASE_RENEW)


@app.on_event("startup")
def _startup_resume():
    global role
    if HA_ENABLED:
        role = "standby"                               # never assume primary before the lease decides
        threading.Thread(target=_lease_supervisor, daemon=True).start()
    else:
        _resume_if_needed()


@app.get("/status")
def get_status():
    return {**system_status, "role": role, "primary": primary_node, "node": NODE_ID}


@app.get("/health")
def health_check():
    """On-demand health probe of all workers.

    Does not write to the event timeline (log_events=False); the frontend
    displays the result in a popup instead. Returns per-worker details so the
    popup can show name, address and status without cross-referencing /status.
    """
    global WORKERS
    WORKERS = discover_tailscale_workers()
    results = probe_all_workers(log_events=False)

    workers = []
    for w in WORKERS:
        wid = w["id"]
        alive = results.get(wid, False)
        workers.append({
            "id": wid,
            "name": w["name"],
            "address": w["address"],
            "health": "online" if alive else "offline",
        })

    online = sum(1 for w in workers if w["health"] == "online")
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "online": online,
        "total": len(workers),
        "workers": workers,
    }


@app.get("/download/{job_id}")
def download_video(job_id: str):
    final_path = os.path.abspath(os.path.join("jobs", job_id, "final_output.mp4"))
    if not os.path.isfile(final_path):
        return {"error": "File not found. Rendering may still be in progress."}
    return FileResponse(
        final_path,
        media_type="video/mp4",
        filename="rendered_output.mp4",
    )


# ---------------------------------------------------------------------------
# Serve the built frontend (must be mounted LAST, after all API routes, so the
# API endpoints above take precedence). This lets one URL — e.g. a Tailscale
# Funnel public URL — serve both the dashboard and the API on the same origin,
# so uploads/downloads work from any device without exposing tailnet IPs.
# Put the built `dist/` folder next to where the master runs.
# ---------------------------------------------------------------------------
if os.path.isdir("dist"):
    app.mount("/", StaticFiles(directory="dist", html=True), name="frontend")