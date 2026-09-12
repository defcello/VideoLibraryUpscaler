"""Stage 0: copy the source file from the NAS onto fast local scratch storage.

Picks between the configured staging drives (typically `R:\\` a RAM disk, then
`Z:\\` a fixed volume) by checking *live* free space -- see config.json's
`staging_free_space_margin_gb`. In practice this almost always resolves to the
larger drive since a RAM disk rarely has room for a video file; when it does
pick a RAM disk we log a warning since that storage is volatile (wiped on
reboot) and the job would need to restart from Stage 0 if the machine restarts
mid-job.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .. import db, naming
from ..config import CONFIG

STAGE = "staged"


def _pick_staging_drive(required_bytes: int) -> str:
    margin = CONFIG["staging_free_space_margin_gb"] * (1024 ** 3)
    for drive in CONFIG["staging_drives"]:
        try:
            usage = shutil.disk_usage(drive)
        except OSError:
            continue
        if usage.free - required_bytes > margin:
            return drive
    # Nothing comfortably fits -- fall back to the last configured drive and
    # let the copy fail loudly if it truly doesn't fit, rather than silently
    # picking a drive we already know is too small.
    return CONFIG["staging_drives"][-1]


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    if job is None:
        raise RuntimeError(f"job {job_id} not found")

    src = Path(job["original_nas_path"])
    if not src.exists():
        raise FileNotFoundError(f"source not found on NAS: {src}")

    if job["skip_upscale"] and naming.has_progressive_res_tag(job["original_filename"]):
        db.log(job_id, STAGE, "skip_upscale set and source is already tagged with a progressive "
                              "resolution (e.g. [1080p]) -- nothing to do, no-op")
        db.update_job(job_id, stage="finalized", status="done", current_file=str(src))
        return

    size = src.stat().st_size
    drive = _pick_staging_drive(size)
    if drive.rstrip("\\/").upper().startswith("R:"):
        db.log(job_id, STAGE, f"WARNING: staging on {drive} (RAM disk) -- volatile, wiped on reboot")

    staging_dir = Path(drive) / CONFIG["staging_subdir"] / job_id
    staging_dir.mkdir(parents=True, exist_ok=True)

    working_name = naming.sanitize_working_name(job_id, src.suffix)
    dest = staging_dir / working_name

    db.log(job_id, STAGE, f"copying {src} ({size / 1e9:.2f} GB) -> {dest}")
    shutil.copyfile(src, dest)
    db.log(job_id, STAGE, "copy complete")

    db.update_job(
        job_id,
        stage=STAGE,
        status="pending",
        staging_drive=drive,
        staging_dir=str(staging_dir),
        working_name=working_name,
        current_file=str(dest),
    )
