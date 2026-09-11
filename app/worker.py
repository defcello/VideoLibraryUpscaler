"""Background worker: processes one job at a time (single GPU) through the
stage pipeline, dispatching on each job's last-completed `stage` so a crash
mid-stage just redoes that one stage on restart -- see db.py's STAGES.
"""
from __future__ import annotations

import threading
import time
import traceback

from . import db
from .stages import deinterlace, denoise, finalize, ingest, probe, upscale

STAGE_RUNNERS = {
    "queued": ingest.run,
    "staged": probe.run,
    "probed": deinterlace.run,
    "deinterlaced": denoise.run,
    "denoised": upscale.run,
    "upscaled": finalize.run,
}

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_current_job_id: str | None = None


def current_job_id() -> str | None:
    return _current_job_id


def _process_one(job_row) -> None:
    global _current_job_id
    job_id = job_row["id"]
    _current_job_id = job_id
    stage = job_row["stage"]
    runner = STAGE_RUNNERS.get(stage)
    if runner is None:
        db.log(job_id, stage, f"no runner for stage '{stage}' -- marking failed")
        db.update_job(job_id, status="failed", error_message=f"no runner for stage '{stage}'")
        return

    db.update_job(job_id, status="running", error_message=None)
    db.log(job_id, stage, f"--- starting stage after '{stage}' ---")
    try:
        runner(job_id)
    except Exception as e:  # noqa: BLE001 -- surface every failure to the manifest, never crash the worker loop
        tb = traceback.format_exc()
        db.log(job_id, stage, f"FAILED: {e}\n{tb}")
        db.update_job(job_id, status="failed", error_message=str(e))
    finally:
        _current_job_id = None


def _loop(poll_seconds: float) -> None:
    print("[worker] loop started")
    while not _stop_event.is_set():
        job_row = db.next_queued_job()
        if job_row is None:
            _stop_event.wait(poll_seconds)
            continue
        _process_one(job_row)


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
