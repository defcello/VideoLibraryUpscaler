"""Stage 5: embed the job's full processing-history metadata, tag the final
filename, then move the finished master back to the source's NAS folder and
reclaim local scratch space. The original source file is never touched or
deleted -- it's archival and may be reprocessed as tools improve.

This is now the ONLY place final metadata gets embedded and the ONLY place
the final filename tag gets decided. Previously that was split between
deinterlace.py (when `skip_upscale` made it the assumed-terminal stage) and
upscale.py -- that assumption broke once denoise/dehalo became independent
toggles that can run (or not) regardless of whether upscale itself runs, so
"which stage is last" is no longer fixed. Centralizing it here means every
combination of toggles produces a correctly-tagged, correctly-metadata'd
deliverable without each stage needing to know whether it's "the last one".

Always does one cheap stream-copy remux (no re-encode) to attach metadata and
standardize on .mkv -- MP4's metadata model silently drops custom
`-metadata` tags (see CLAUDE.md's ffmpeg gotchas), so every delivered file
uses Matroska regardless of which stages actually ran or what container they
left the file in.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .. import db, staging
from ..config import CONFIG
from ..decoder_util import cuvid_decoder_args
from ..metadata_tags import ffmpeg_metadata_args, processed_date
from ..naming import join_tags, set_final_progressive_tag, set_upscaled_tag, split_tags
from ..procutil import run_logged

STAGE = "finalized"

FFMPEG = CONFIG["tools"]["ffmpeg"]


def _scan_summary(settings: dict) -> str:
    conf = settings.get("confidence")
    conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "n/a"
    return f"{settings.get('scan_type', '?')} (tff={settings.get('tff')}, confidence={conf_str})"


def _unique_destination(dest: Path) -> Path:
    if not dest.exists():
        return dest
    base, tags, ext = split_tags(dest.name)
    n = 2
    while True:
        candidate = dest.with_name(join_tags(f"{base} ({n})", tags, ext))
        if not candidate.exists():
            return candidate
        n += 1


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    settings = db.get_settings(job_id)
    current = Path(job["current_file"])

    upscaled_height = settings.get("upscaled_height")
    if not job["skip_upscale"] and upscaled_height:
        final_name = set_upscaled_tag(job["original_filename"], upscaled_height)
    else:
        extra_tags = []
        if settings.get("denoise_summary", "skipped") != "skipped":
            extra_tags.append("Denoised")
        if settings.get("dehalo_summary", "skipped") != "skipped":
            extra_tags.append("Dehalo")
        final_name = set_final_progressive_tag(
            job["original_filename"], upscaled_height or settings.get("height", 0), extra_tags
        )
    # Always deliver as .mkv regardless of the source's own container or
    # whatever extension the last stage happened to leave the file in.
    final_name = str(Path(final_name).with_suffix(".mkv"))

    meta = {
        "TOOL": "AI Remaster Pipeline",
        "SOURCE_FILE": job["original_filename"],
        "SCAN_DETECTION": _scan_summary(settings),
        "DEINTERLACE": settings.get("deinterlace_summary", "skipped"),
        "DENOISE": settings.get("denoise_summary", "skipped"),
        "DEHALO": settings.get("dehalo_summary", "skipped"),
        "UPSCALE": settings.get("upscale_summary", "skipped"),
        "PROCESSED": processed_date(),
    }

    remuxed = current.with_name(current.stem + "_final.mkv")
    cmd = [
        FFMPEG, "-hide_banner", "-y",
        *cuvid_decoder_args(current), "-i", str(current),
        "-c", "copy", *ffmpeg_metadata_args(meta),
        str(remuxed),
    ]
    db.log(job_id, STAGE, f"embedding final processing-history metadata -> {remuxed}")
    run_logged(job_id, STAGE, cmd)
    if not remuxed.exists() or remuxed.stat().st_size == 0:
        raise RuntimeError("finalize metadata remux produced no output file")

    dest_dir = Path(job["original_nas_path"]).parent
    dest_path = _unique_destination(dest_dir / final_name)

    db.log(job_id, STAGE, f"moving {remuxed} -> {dest_path}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(remuxed), str(dest_path))
    db.log(job_id, STAGE, f"finalized: {dest_path}")
    db.update_job(job_id, stage=STAGE, status="done", current_file=str(dest_path), completed_at=time.time())
    staging.cleanup(job_id)
