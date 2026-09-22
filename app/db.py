"""SQLite-backed job manifest -- the single source of truth for pipeline state.

A job moves through STAGES in order. Each stage only advances `stage` after its
output file exists on disk, so a crash mid-stage just redoes that one stage on
restart (see worker.py). `needs_review` and `failed` are terminal-ish states
that pause a job for a human decision without losing its place in the queue.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import DB_PATH

STAGES = [
    "queued",
    "staged",
    "probed",
    "deinterlaced",
    "denoised",
    "dehaloed",
    "upscaled",
    "finalized",
]
TERMINAL_STATES = {"finalized", "failed", "needs_review", "cancelled"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    original_nas_path   TEXT NOT NULL,
    original_filename   TEXT NOT NULL,
    working_name        TEXT NOT NULL,
    stage               TEXT NOT NULL DEFAULT 'queued',
    status              TEXT NOT NULL DEFAULT 'pending',   -- pending|running|needs_review|failed|done
    staging_drive       TEXT,
    staging_dir         TEXT,
    current_file        TEXT,                              -- path to the latest-good intermediate/output
    settings_json        TEXT NOT NULL DEFAULT '{}',        -- detected + chosen settings, merged over stages
    error_message        TEXT,
    deinterlace_enabled   INTEGER NOT NULL DEFAULT 1,
    denoise_enabled       INTEGER NOT NULL DEFAULT 0,
    dehalo_enabled        INTEGER NOT NULL DEFAULT 0,
    denoise_tune          TEXT NOT NULL DEFAULT 'none',
    topaz_preset           TEXT NOT NULL DEFAULT 'topaz_default',
    skip_upscale            INTEGER NOT NULL DEFAULT 0,
    crop_start_seconds      REAL,
    crop_end_seconds        REAL,
    failure_category        TEXT,                            -- 'oom' | 'disk_full' | NULL, set on status='failed'
    progress_percent        REAL,                             -- 0-100 within the currently-running stage, NULL if unknown
    queue_order              INTEGER,                          -- manual position among not-yet-'done' jobs (see reorder_job); irrelevant once status='done'
    completed_at             REAL,                             -- set once, alongside status='done' (see finalize.py); NULL until then
    created_at             REAL NOT NULL,
    updated_at              REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS job_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    ts          REAL NOT NULL,
    stage       TEXT NOT NULL,
    message     TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id);
"""


# A single shared connection guarded by a lock, rather than a fresh
# connection per call. Stages call db.log() once per subprocess output line
# -- potentially thousands of times per job -- and rapid open/close churn on
# a WAL-mode database proved flaky on Windows ("attempt to write a readonly
# database" under concurrent access from the worker thread + SSE polling).
# Our actual concurrency needs are modest (one worker thread + occasional web
# requests), so a process-wide lock is simple and robust rather than clever.
_conn: Optional[sqlite3.Connection] = None
_conn_lock = threading.Lock()


def _shared_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.execute("PRAGMA busy_timeout=30000")
    return _conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    with _conn_lock:
        conn = _shared_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight ALTER-if-missing migration for columns added after the
    table already existed on disk (CREATE TABLE IF NOT EXISTS won't add them
    to an existing table)."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "skip_upscale" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN skip_upscale INTEGER NOT NULL DEFAULT 0")
    if "crop_start_seconds" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN crop_start_seconds REAL")
    if "crop_end_seconds" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN crop_end_seconds REAL")
    if "failure_category" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN failure_category TEXT")
    if "deinterlace_enabled" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN deinterlace_enabled INTEGER NOT NULL DEFAULT 1")
    if "dehalo_enabled" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN dehalo_enabled INTEGER NOT NULL DEFAULT 0")
    if "progress_percent" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN progress_percent REAL")
    if "queue_order" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN queue_order INTEGER")
        # Backfill so existing rows keep their current (created_at) order the
        # first time this runs, rather than all landing on the same NULL and
        # sorting arbitrarily.
        for i, row in enumerate(conn.execute("SELECT id FROM jobs ORDER BY created_at ASC")):
            conn.execute("UPDATE jobs SET queue_order = ? WHERE id = ?", (i, row["id"]))
    if "completed_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN completed_at REAL")
        # Best-effort backfill for jobs already 'done' before this column
        # existed -- updated_at is untouched after finalize.py's terminal
        # update, so it's a reasonable stand-in for the real completion time.
        conn.execute("UPDATE jobs SET completed_at = updated_at WHERE status = 'done' AND completed_at IS NULL")


def create_job(
    original_nas_path: str,
    original_filename: str,
    working_name: str,
    denoise_enabled: bool,
    denoise_tune: str,
    topaz_preset: str,
    skip_upscale: bool = False,
    deinterlace_enabled: bool = True,
    dehalo_enabled: bool = False,
    crop_start_seconds: Optional[float] = None,
    crop_end_seconds: Optional[float] = None,
) -> str:
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with get_conn() as conn:
        # New jobs land at the bottom of the not-yet-done queue, same as the
        # old created_at-ascending default -- done jobs are excluded since
        # their queue_order is meaningless once they're sorted by completed_at.
        next_order = conn.execute(
            "SELECT COALESCE(MAX(queue_order), -1) + 1 FROM jobs WHERE status != 'done'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO jobs (
                id, original_nas_path, original_filename, working_name,
                stage, status, settings_json,
                deinterlace_enabled, denoise_enabled, denoise_tune, dehalo_enabled,
                topaz_preset, skip_upscale,
                crop_start_seconds, crop_end_seconds, queue_order,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', 'pending', '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id, original_nas_path, original_filename, working_name,
                int(deinterlace_enabled), int(denoise_enabled), denoise_tune, int(dehalo_enabled),
                topaz_preset, int(skip_upscale),
                crop_start_seconds, crop_end_seconds, next_order,
                now, now,
            ),
        )
    return job_id


def get_job(job_id: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def list_jobs(status: Optional[str] = None) -> list[sqlite3.Row]:
    with get_conn() as conn:
        if status:
            return conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC", (status,)
            ).fetchall()
        return conn.execute("SELECT * FROM jobs ORDER BY created_at ASC").fetchall()


# Jobs are never done being re-sorted after just one comparison key, so this
# ORDER BY is shared verbatim between the dashboard listing and the worker's
# own pick-next-job query -- see list_jobs_ordered() and next_queued_job().
_QUEUE_ORDER_SQL = """
    CASE WHEN status = 'done' THEN 0 ELSE 1 END,
    CASE WHEN status = 'done' THEN completed_at ELSE queue_order END ASC,
    created_at ASC
"""


def list_jobs_ordered() -> list[sqlite3.Row]:
    """Dashboard listing order: every 'done' job first, oldest completion to
    newest, then every other job (queued/running/failed/needs_review/...) in
    manual queue order -- see reorder_job(). This is also the order the
    worker actually processes pending jobs in (next_queued_job)."""
    with get_conn() as conn:
        return conn.execute(f"SELECT * FROM jobs ORDER BY {_QUEUE_ORDER_SQL}").fetchall()


def next_queued_job() -> Optional[sqlite3.Row]:
    """The not-currently-running job next up in queue order (see reorder_job)
    among those that are actually ready to process."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY queue_order ASC, created_at ASC LIMIT 1"
        ).fetchone()


def reorder_job(job_id: str, direction: Optional[str] = None, row: Optional[int] = None) -> None:
    """Repositions job_id among the other not-yet-'done' jobs. Completed jobs
    are never part of this ordering -- they're always pinned to the top by
    completed_at instead (see list_jobs_ordered) -- so both `row` (a 1-based
    position in the full dashboard table, completed jobs included) and the
    `direction` shortcuts ("top"/"up"/"down"/"bottom") operate purely on the
    not-done sublist, with `row` clamped into the range below however many
    completed jobs currently sit above it. Exactly one of `direction`/`row`
    should be given; renumbers the whole not-done sublist as consecutive
    integers via a raw UPDATE (not update_job()) so this doesn't bump
    updated_at on every other queued job just because one of them moved.
    """
    with get_conn() as conn:
        full = conn.execute(f"SELECT id, status FROM jobs ORDER BY {_QUEUE_ORDER_SQL}").fetchall()
        completed_count = sum(1 for r in full if r["status"] == "done")
        incomplete_ids = [r["id"] for r in full if r["status"] != "done"]
        if job_id not in incomplete_ids:
            return  # not found, or a completed job -- nothing to reorder
        i = incomplete_ids.index(job_id)
        n = len(incomplete_ids)
        if direction == "top":
            new_idx = 0
        elif direction == "up":
            new_idx = max(0, i - 1)
        elif direction == "down":
            new_idx = min(n - 1, i + 1)
        elif direction == "bottom":
            new_idx = n - 1
        elif row is not None:
            new_idx = min(max(row - completed_count - 1, 0), n - 1)
        else:
            return
        if new_idx == i:
            return
        incomplete_ids.pop(i)
        incomplete_ids.insert(new_idx, job_id)
        conn.executemany(
            "UPDATE jobs SET queue_order = ? WHERE id = ?",
            [(pos, jid) for pos, jid in enumerate(incomplete_ids)],
        )


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", values)


def merge_settings(job_id: str, patch: dict) -> dict:
    """Shallow-merge `patch` into the job's settings_json blob and persist it."""
    job = get_job(job_id)
    current = json.loads(job["settings_json"]) if job and job["settings_json"] else {}
    current.update(patch)
    update_job(job_id, settings_json=json.dumps(current))
    return current


def get_settings(job_id: str) -> dict:
    job = get_job(job_id)
    if not job or not job["settings_json"]:
        return {}
    return json.loads(job["settings_json"])


def log(job_id: str, stage: str, message: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO job_logs (job_id, ts, stage, message) VALUES (?, ?, ?, ?)",
            (job_id, time.time(), stage, message),
        )


def get_logs(job_id: str, limit: int = 500) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM job_logs WHERE job_id = ? ORDER BY id DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()


def delete_job(job_id: str) -> None:
    """Removes a job's record (and its logs) from the manifest so the queue
    dashboard doesn't get cluttered. Does NOT touch any file on disk -- the
    finished output already lives wherever finalize.py moved it, and this
    only clears the tracking row."""
    with get_conn() as conn:
        conn.execute("DELETE FROM job_logs WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def recover_running_jobs() -> int:
    """On startup, any job stuck in 'running' means the process died mid-stage.
    Its `stage` field only advances after a stage's output is confirmed
    written (see each stages/*.py `run()`), so resetting status back to
    'pending' safely re-runs just that one stage."""
    with get_conn() as conn:
        cur = conn.execute("UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'running'", (time.time(),))
        return cur.rowcount


def recover_needs_restart_jobs() -> int:
    """On startup, jobs parked as 'needs_restart' (the generative engine's
    VRAM pre-check failed, likely fragmentation that only a restart clears)
    are safe to retry automatically now that a restart has actually
    happened -- unlike 'paused' (see recover_paused_jobs), which is a
    deliberate user action and must never auto-resume just because the
    server restarted."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'needs_restart'", (time.time(),)
        )
        return cur.rowcount


def recover_paused_jobs() -> int:
    """Flips any 'paused' job(s) back to 'pending' -- called only from the
    explicit POST /api/worker/resume action, never from server startup, since
    a user who paused to free the GPU shouldn't have that work silently
    resume just because the server process restarted."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'paused'", (time.time(),)
        )
        return cur.rowcount


def next_stage(current: str) -> str:
    idx = STAGES.index(current)
    return STAGES[min(idx + 1, len(STAGES) - 1)]
