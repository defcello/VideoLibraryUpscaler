"""FastAPI app: job submission, queue dashboard (via SSE), NAS path browsing,
and review/retry actions. Run with `python -m app.server` or via uvicorn --
see README.md."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, worker
from .config import CONFIG, STATIC_DIR, list_presets, load_preset

app = FastAPI(title="AI Remaster Pipeline")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    recovered = db.recover_running_jobs()
    if recovered:
        print(f"[server] recovered {recovered} job(s) stuck in 'running' after a restart")
    worker.start()


# ---------------------------------------------------------------- job models

class CreateJobsRequest(BaseModel):
    paths: list[str]
    denoise_enabled: bool = False
    denoise_tune: str = "none"
    topaz_preset: str = CONFIG["default_topaz_preset"]


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
    created = []
    for p in req.paths:
        src = Path(p)
        if not src.exists():
            raise HTTPException(400, f"path not found: {p}")
        job_id = db.create_job(
            original_nas_path=str(src),
            original_filename=src.name,
            working_name="",  # filled in by the ingest stage
            denoise_enabled=req.denoise_enabled,
            denoise_tune=req.denoise_tune,
            topaz_preset=req.topaz_preset,
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


@app.post("/api/jobs/{job_id}/retry")
def api_retry_job(job_id: str):
    row = db.get_job(job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    if row["status"] not in ("failed", "needs_review"):
        raise HTTPException(400, "only failed/needs_review jobs can be retried")
    db.update_job(job_id, status="pending", error_message=None)
    return {"ok": True}


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

@app.get("/api/presets")
def api_presets():
    return {
        "topaz_presets": {name: load_preset(name) for name in list_presets()},
        "denoise_tunes": load_preset("denoise_tunes"),
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
            payload = json.dumps({"jobs": jobs, "current_job_id": worker.current_job_id()})
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
