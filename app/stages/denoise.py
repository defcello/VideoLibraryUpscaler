"""Stage 3 (optional): HandBrakeCLI NLMeans denoise, Strong preset, tune
chosen per batch at submission time (None/Film/Animation). Re-encodes with
nvenc_h264 since applying a filter requires a re-encode; audio passed through
untouched."""
from __future__ import annotations

from pathlib import Path

from .. import db, naming
from ..config import CONFIG, load_preset
from ..procutil import run_logged

STAGE = "denoised"

HANDBRAKE = CONFIG["tools"]["handbrake_cli"]


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    settings = db.get_settings(job_id)
    src = Path(job["current_file"])

    if not job["denoise_enabled"]:
        db.log(job_id, STAGE, "denoise disabled for this job -- passing through unchanged")
        db.update_job(job_id, stage=STAGE, status="pending")
        return

    tunes = load_preset("denoise_tunes")
    tune_key = job["denoise_tune"] or CONFIG["default_denoise_tune"]
    tune = tunes["tunes"].get(tune_key, tunes["tunes"]["none"])
    nlmeans_preset = tunes["nlmeans_preset"]

    out_path = src.with_name(src.stem + "_denoised.mp4")

    cmd = [
        HANDBRAKE,
        "-i", str(src),
        "-o", str(out_path),
        "--nlmeans", nlmeans_preset,
        "--nlmeans-tune", tune["handbrake_tune"],
        "-e", "nvenc_h264", "-q", "18",
        "--all-audio", "-E", "copy",
    ]
    db.log(job_id, STAGE, f"denoise tune={tune_key} ({tune['label']})")
    run_logged(job_id, STAGE, cmd)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("denoise stage produced no output file")

    display_name = settings.get("display_filename") or job["original_filename"]
    new_name = naming.add_denoised_tag(display_name)

    db.log(job_id, STAGE, f"denoise complete -> {out_path}")
    db.update_job(job_id, stage=STAGE, status="pending", current_file=str(out_path))
    db.merge_settings(job_id, {"display_filename": new_name})
