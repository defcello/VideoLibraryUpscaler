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
from ..decoder_util import cuvid_decoder_args
from ..procutil import run_logged

STAGE = "staged"

FFMPEG = CONFIG["tools"]["ffmpeg"]


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


def _apply_test_crop(job_id: str, staged_file: Path, start_seconds: float, end_seconds: float | None) -> Path:
    """Testing aid: trims the staged copy down to an In/Out range before it
    enters the rest of the pipeline, so a slow preset (e.g. the generative
    Starlight engine) can be iterated on with a short clip instead of a full
    episode. Uses `-c copy` (no re-encode, no decoder concerns) -- the actual
    cut point snaps to the nearest preceding keyframe, which is an acceptable
    tradeoff for a testing feature but means the clip may start a little
    earlier than requested."""
    trimmed = staged_file.with_name(staged_file.stem + "_crop" + staged_file.suffix)
    # Topaz's ffmpeg has no software h264/hevc decoder -- its hwaccel auto-pick
    # defaults to QSV and fails hard on this NVIDIA-only machine even for a
    # `-c copy` trim (ffmpeg still opens a decoder for accurate -ss seeking),
    # same gotcha decoder_util.py works around elsewhere.
    cmd = [FFMPEG, "-hide_banner", "-y", *cuvid_decoder_args(staged_file), "-ss", str(start_seconds), "-i", str(staged_file)]
    if end_seconds is not None:
        cmd += ["-t", str(end_seconds - start_seconds)]
    cmd += ["-c", "copy", str(trimmed)]
    db.log(job_id, STAGE, f"test crop enabled: {start_seconds}s -> {end_seconds if end_seconds is not None else 'end'}")
    run_logged(job_id, STAGE, cmd)
    if not trimmed.exists() or trimmed.stat().st_size == 0:
        raise RuntimeError("test crop produced no output file")
    staged_file.unlink(missing_ok=True)
    return trimmed


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

    crop_start = job["crop_start_seconds"]
    crop_end = job["crop_end_seconds"]
    if crop_start is not None or crop_end is not None:
        dest = _apply_test_crop(job_id, dest, crop_start or 0.0, crop_end)

    db.update_job(
        job_id,
        stage=STAGE,
        status="pending",
        staging_drive=drive,
        staging_dir=str(staging_dir),
        working_name=working_name,
        current_file=str(dest),
    )
