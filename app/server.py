"""FastAPI app: job submission, queue dashboard (via SSE), NAS path browsing,
and review/retry actions. Run with `python -m app.server` or via uvicorn --
see README.md."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, procutil, worker
from .config import CONFIG, STATIC_DIR, list_presets, load_preset
from .topaz_models import available_scales, describe_upscale_strategy

app = FastAPI(title="AI Remaster Pipeline")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    # This is a single-operator local tool under active iteration -- always
    # serve the latest static files rather than fighting stale browser caches
    # of app.js/style.css after every edit.
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    recovered = db.recover_running_jobs()
    if recovered:
        print(f"[server] recovered {recovered} job(s) stuck in 'running' after a restart")
    restarted = db.recover_needs_restart_jobs()
    if restarted:
        print(f"[server] re-queued {restarted} job(s) parked as 'needs_restart' after a restart")
    # Deliberately NOT recovering 'paused' jobs here -- pausing is a manual
    # user action to free the GPU, and auto-resuming that work just because
    # the server process restarted would defeat the point. Only the explicit
    # POST /api/worker/resume does that.
    worker.start()


# ---------------------------------------------------------------- job models

class CreateJobsRequest(BaseModel):
    paths: list[str]
    deinterlace_enabled: bool = True
    denoise_enabled: bool = False
    dehalo_enabled: bool = False
    content_type: str = load_preset("content_types")["default"]
    skip_upscale: bool = False
    crop_start_seconds: Optional[float] = None
    crop_end_seconds: Optional[float] = None


class ReviewDecisionRequest(BaseModel):
    scan_type: Optional[str] = None   # "progressive" | "interlaced" | "telecine"
    tff: Optional[bool] = None
    proceed: bool = True


def _row_to_dict(row) -> dict:
    d = dict(row)
    if "settings_json" in d and d["settings_json"]:
        d["settings"] = json.loads(d["settings_json"])
    else:
        d["settings"] = {}
    d.pop("settings_json", None)
    return d


# --------------------------------------------------------------------- jobs

@app.get("/api/jobs")
def api_list_jobs():
    return [_row_to_dict(r) for r in db.list_jobs()]


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str):
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    d = _row_to_dict(row)
    d["logs"] = [dict(l) for l in reversed(db.get_logs(job_id, limit=1000))]
    return d


@app.post("/api/jobs")
def api_create_jobs(req: CreateJobsRequest):
    content_types = load_preset("content_types")["types"]
    if req.content_type not in content_types:
        print(f"[api_create_jobs] 400: unknown content_type={req.content_type!r} paths={req.paths}")
        raise HTTPException(400, f"unknown content_type: {req.content_type}")
    resolved = content_types[req.content_type]

    if req.crop_start_seconds is not None and req.crop_end_seconds is not None:
        if req.crop_end_seconds <= req.crop_start_seconds:
            print(f"[api_create_jobs] 400: crop end<=start ({req.crop_start_seconds}, {req.crop_end_seconds}) paths={req.paths}")
            raise HTTPException(400, "crop end must be after crop start")

    created = []
    for p in req.paths:
        src = Path(p)
        if not src.exists():
            print(f"[api_create_jobs] 400: path not found: {p!r} (all paths: {req.paths})")
            raise HTTPException(400, f"path not found: {p}")
        job_id = db.create_job(
            original_nas_path=str(src),
            original_filename=src.name,
            working_name="",  # filled in by the ingest stage
            deinterlace_enabled=req.deinterlace_enabled,
            denoise_enabled=req.denoise_enabled,
            dehalo_enabled=req.dehalo_enabled,
            denoise_tune=resolved["denoise_tune"],
            topaz_preset=resolved["topaz_preset"],
            skip_upscale=req.skip_upscale,
            crop_start_seconds=req.crop_start_seconds,
            crop_end_seconds=req.crop_end_seconds,
        )
        created.append(job_id)
    return {"created": created}


@app.post("/api/jobs/{job_id}/review")
def api_review_job(job_id: str, decision: ReviewDecisionRequest):
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    if row["status"] != "needs_review":
        raise HTTPException(400, "job is not awaiting review")

    patch = {}
    if decision.scan_type is not None:
        patch["scan_type"] = decision.scan_type
    if decision.tff is not None:
        patch["tff"] = decision.tff
    if patch:
        db.merge_settings(job_id, patch)

    if decision.proceed:
        db.update_job(job_id, status="pending", error_message=None)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def api_delete_job(job_id: str):
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    if row["status"] == "running":
        worker.request_cancel(job_id)
        # kill_active() only signals the subprocess -- give the worker
        # thread a brief moment to notice, record the cancellation, and let
        # go of the job before we delete its row out from under it.
        for _ in range(50):  # up to ~5s
            time.sleep(0.1)
            if worker.current_job_id() != job_id:
                break
    staging_dir = row["staging_dir"]
    if staging_dir and Path(staging_dir).exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    db.delete_job(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/abort")
def api_abort_job(job_id: str):
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    if not worker.request_cancel(job_id):
        raise HTTPException(400, "job is already finished -- nothing to abort")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/rerun")
def api_rerun_job(job_id: str):
    """Duplicates a job's original submission parameters into a brand new
    job and queues it immediately -- the original job (and its output, if
    any) is left untouched."""
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    src = Path(row["original_nas_path"])
    if not src.exists():
        raise HTTPException(400, f"original source no longer exists: {src}")
    new_job_id = db.create_job(
        original_nas_path=row["original_nas_path"],
        original_filename=row["original_filename"],
        working_name="",
        deinterlace_enabled=bool(row["deinterlace_enabled"]),
        denoise_enabled=bool(row["denoise_enabled"]),
        dehalo_enabled=bool(row["dehalo_enabled"]),
        denoise_tune=row["denoise_tune"],
        topaz_preset=row["topaz_preset"],
        skip_upscale=bool(row["skip_upscale"]),
        crop_start_seconds=row["crop_start_seconds"],
        crop_end_seconds=row["crop_end_seconds"],
    )
    return {"created": new_job_id}


@app.post("/api/jobs/{job_id}/retry")
def api_retry_job(job_id: str):
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    if row["status"] not in ("failed", "needs_review", "needs_restart"):
        raise HTTPException(400, "only failed/needs_review/needs_restart jobs can be retried")
    db.update_job(job_id, status="pending", error_message=None)
    return {"ok": True}


# --------------------------------------------------------------- worker control

@app.post("/api/worker/pause")
def api_worker_pause():
    procutil.request_pause()
    return {"ok": True, "state": procutil.pause_state()}


@app.post("/api/worker/resume")
def api_worker_resume():
    procutil.request_resume()
    resumed = db.recover_paused_jobs()
    return {"ok": True, "state": procutil.pause_state(), "resumed_jobs": resumed}


# ----------------------------------------------------------------- browsing

@app.get("/api/browse")
def api_browse(path: Optional[str] = None):
    target = Path(path) if path else Path(CONFIG["nas_root"])
    if not target.exists() or not target.is_dir():
        raise HTTPException(400, f"not a directory: {target}")
    entries = []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name.lower())):
            entries.append({
                "name": entry.name,
                "path": str(Path(entry.path)),
                "is_dir": entry.is_dir(),
                "size": None if entry.is_dir() else entry.stat().st_size,
            })
    except PermissionError:
        raise HTTPException(403, f"permission denied: {target}")
    parent = str(target.parent) if target.parent != target else None
    return {"path": str(target), "parent": parent, "entries": entries}


# ------------------------------------------------------------------ presets

def _preset_display(preset: dict) -> dict:
    """Human-readable summary of a Topaz preset's processing stack, computed
    from the same logic upscale.py actually uses -- so the UI's preset panel
    can never drift out of sync with what a job would really do."""
    enc = preset["encoder"]
    if preset.get("engine") == "neuroserver":
        # Generative (Starlight) presets use a completely different engine
        # (neuroserver.exe, not ffmpeg's tvai_up) -- available_scales() /
        # describe_upscale_strategy() assume the classic tvai_up model JSON
        # shape and don't apply here (astrasharp.json's own "scales" field is
        # actually misleading for this model -- see topaz_film_generative.json).
        return {
            "model_label": f"{preset['model_display_name']} ({preset['ai_model_label']} → {preset['model_variant_label']})",
            "target_tier": f"{preset['output_tier_height']}p or higher",
            "available_scales": None,
            "upscale_strategy": (
                "Single-pass generative upscale: requests the smallest integer scale "
                "that reaches the target tier (e.g. 3x for a 480p source). The actual "
                "output height is measured from the result rather than assumed, since "
                "this model's real output can land below the nominally requested scale."
            ),
            "precleanup_enabled": False,
            "precleanup_label": None,
            "resize_flags": None,
            "encoder_label": "Topaz-managed (neuroserver internal encode, see preset's ffmpeg_encoding)",
            "container": enc["container"].upper(),
            "audio_mode": enc["audio_mode"],
        }
    scales = available_scales(preset["model"])
    pc = preset.get("precleanup", {})
    return {
        "model_label": f"{preset['model_display_name']} ({preset['model']})",
        "target_tier": f"{preset['output_tier_height']}p",
        "available_scales": scales,
        "upscale_strategy": describe_upscale_strategy(preset["model"]),
        "precleanup_enabled": bool(pc.get("enabled")),
        "precleanup_label": f"{pc.get('model_display_name', '?')} ({pc.get('model', '?')})" if pc.get("enabled") else None,
        "resize_flags": preset.get("resize_flags", "bicubic"),
        "encoder_label": f"{enc['codec']} ({enc['profile']} profile, {enc['bitrate_mode']}, CQ {enc['cq']})",
        "container": enc["container"].upper(),
        "audio_mode": enc["audio_mode"],
    }


@app.get("/api/presets")
def api_presets():
    topaz_presets = {name: load_preset(name) for name in list_presets()}
    return {
        "topaz_presets": topaz_presets,
        "topaz_preset_display": {name: _preset_display(p) for name, p in topaz_presets.items()},
        "denoise_tunes": load_preset("denoise_tunes"),
        "content_types": load_preset("content_types"),
        "default_topaz_preset": CONFIG["default_topaz_preset"],
        "default_denoise_tune": CONFIG["default_denoise_tune"],
        "nas_root": CONFIG["nas_root"],
    }


# ----------------------------------------------------------------- live SSE

@app.get("/api/stream")
async def api_stream():
    async def event_gen():
        last_payload = None
        while True:
            jobs = [_row_to_dict(r) for r in db.list_jobs()]
            payload = json.dumps({
                "jobs": jobs,
                "current_job_id": worker.current_job_id(),
                "worker_pause_state": procutil.pause_state(),
            })
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            await asyncio.sleep(1.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ------------------------------------------------------------------- static

@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn
    uvicorn.run("app.server:app", host="127.0.0.1", port=8756, reload=False)


if __name__ == "__main__":
    main()
