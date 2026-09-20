"""Background worker: processes one job at a time (single GPU) through the
stage pipeline, dispatching on each job's last-completed `stage` so a crash
mid-stage just redoes that one stage on restart -- see db.py's STAGES.
"""
from __future__ import annotations

import threading
import time
import traceback

from . import db, procutil
from .stages import deinterlace, dehalo, denoise, finalize, ingest, probe, upscale

STAGE_RUNNERS = {
    "queued": ingest.run,
    "staged": probe.run,
    "probed": deinterlace.run,
    "deinterlaced": denoise.run,
    "denoised": dehalo.run,
    "dehaloed": upscale.run,
    "upscaled": finalize.run,
}

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_current_job_id: str | None = None

# Set by request_cancel() when an abort is requested for the job currently
# running -- checked once its stage raises (killing its subprocess is what
# actually makes that happen) so the failure gets recorded as a deliberate
# cancellation rather than a real error.
_cancel_requested_job_id: str | None = None
_cancel_lock = threading.Lock()


def current_job_id() -> str | None:
    return _current_job_id


def request_cancel(job_id: str) -> bool:
    """Aborts a job: kills its active subprocess if it's the one currently
    running, or marks it cancelled directly if it's only queued. Returns
    False if the job is already in a terminal state (nothing to abort)."""
    global _cancel_requested_job_id
    row = db.get_job(job_id)
    if row is None:
        return False
    if row["status"] == "running" and job_id == _current_job_id:
        with _cancel_lock:
            _cancel_requested_job_id = job_id
        procutil.kill_active()
        return True
    if row["status"] == "pending":
        db.update_job(job_id, status="cancelled", error_message="Cancelled before it started running")
        return True
    return row["status"] not in db.TERMINAL_STATES  # already done/failed/etc -- nothing to abort


def _classify_failure(message: str, tb: str) -> str | None:
    text = f"{message}\n{tb}".lower()
    if "out of memory" in text or "outofmemoryerror" in text:
        return "oom"
    if "no space left on device" in text or "disk full" in text or "not enough space" in text or "errno 28" in text:
        return "disk_full"
    return None


def _process_one(job_row) -> None:
    global _current_job_id, _cancel_requested_job_id
    job_id = job_row["id"]
    _current_job_id = job_id
    stage = job_row["stage"]
    runner = STAGE_RUNNERS.get(stage)
    if runner is None:
        db.log(job_id, stage, f"no runner for stage '{stage}' -- marking failed")
        db.update_job(job_id, status="failed", error_message=f"no runner for stage '{stage}'")
        return

    db.update_job(job_id, status="running", error_message=None, failure_category=None, progress_percent=0)
    db.log(job_id, stage, f"--- starting stage after '{stage}' ---")
    try:
        runner(job_id)
        # A stage can finish successfully even after kill_active() was
        # called on it (the kill lost the race against the process already
        # finishing on its own) -- clear the flag so it doesn't get
        # misattributed to a later, unrelated stage's real failure.
        with _cancel_lock:
            if _cancel_requested_job_id == job_id:
                _cancel_requested_job_id = None
    except Exception as e:  # noqa: BLE001 -- surface every failure to the manifest, never crash the worker loop
        tb = traceback.format_exc()
        with _cancel_lock:
            was_cancelled = _cancel_requested_job_id == job_id
            _cancel_requested_job_id = None
        if was_cancelled:
            db.log(job_id, stage, f"CANCELLED by user during '{stage}'")
            db.update_job(job_id, status="cancelled", error_message="Cancelled by user")
        else:
            category = _classify_failure(str(e), tb)
            db.log(job_id, stage, f"FAILED: {e}\n{tb}")
            db.update_job(job_id, status="failed", error_message=str(e), failure_category=category)
    finally:
        _current_job_id = None


def _loop(poll_seconds: float) -> None:
    print("[worker] loop started")
    while not _stop_event.is_set():
        # Between stage dispatches is always a safe point to freeze at (a
        # generative upscale's own internal segment loop is what handles
        # pausing mid-run -- this thread is fully sequential, so _loop only
        # ever observes the gap between one stage finishing and the next
        # being picked up, never mid-stage).
        if procutil.pause_state() != "running":
            procutil.should_pause_now()
            _stop_event.wait(poll_seconds)
            continue
        job_row = db.next_queued_job()
        if job_row is None:
            _stop_event.wait(poll_seconds)
            continue
        try:
            _process_one(job_row)
        except Exception:  # noqa: BLE001 -- a bug in _process_one's own error handling must
            # never take the whole background worker down with it (confirmed
            # by testing: it did, silently, until this was added -- every job
            # submitted afterward just sat at 'pending' forever with no
            # visible error anywhere except the server's own stderr).
            print(f"[worker] _process_one crashed unexpectedly for job {job_row['id']}:\n{traceback.format_exc()}")
            try:
                db.update_job(job_row["id"], status="failed", error_message="internal worker error -- see server log")
            except Exception:
                pass


def start(poll_seconds: float = 3.0) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_loop, args=(poll_seconds,), daemon=True, name="pipeline-worker")
    _thread.start()


def stop() -> None:
    _stop_event.set()
    if _thread:
        _thread.join(timeout=10)
