"""Stage 2: deinterlace / inverse-telecine to a progressive mezzanine file.

Reuses Hybrid's own bundled, fully portable VapourSynth + QTGMC/VIVTC engine
(see config.json's tools.vspipe / vs_plugins) so the output matches what the
guide's Hybrid-GUI workflow produces, driven headlessly via VSPipe piped into
ffmpeg. Also folds in PAR-to-square-pixels normalization and autodetected
letterbox/pillarbox crop from Stage 1, per the "combine with an earlier step"
allowance -- native aspect ratio is preserved, no forced 16:9 padding.
"""
from __future__ import annotations

from pathlib import Path

from .. import db, naming
from ..config import CONFIG
from ..procutil import run_piped_logged
from ..vpy_render import render

STAGE = "deinterlaced"

VSPIPE = CONFIG["tools"]["vspipe"]
FFMPEG = CONFIG["tools"]["ffmpeg"]


def _template_for(scan_type: str) -> str:
    return {
        "interlaced": "qtgmc_interlaced.vpy.j2",
        "telecine": "ivtc_telecine.vpy.j2",
        "progressive": "passthrough_progressive.vpy.j2",
    }[scan_type]


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    settings = db.get_settings(job_id)
    src = Path(job["current_file"])
    scan_type = settings.get("scan_type", "interlaced")

    template = _template_for(scan_type)
    script_text = render(
        template,
        source_path=str(src),
        tff=settings.get("tff", True),
        crop_left=settings.get("crop_left", 0),
        crop_right=settings.get("crop_right", 0),
        crop_top=settings.get("crop_top", 0),
        crop_bottom=settings.get("crop_bottom", 0),
        par_num=settings.get("par_num", 1),
        par_den=settings.get("par_den", 1),
    )
    script_path = src.with_name(src.stem + "_deint.vpy")
    script_path.write_text(script_text, encoding="utf-8")
    db.log(job_id, STAGE, f"scan_type={scan_type} using template {template}; script written to {script_path}")

    out_path = src.with_name(src.stem + "_deint.mp4")

    ffmpeg_cmd = [
        FFMPEG, "-hide_banner", "-y",
        "-f", "yuv4mpegpipe", "-i", "-",
        "-i", str(src),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr_hq", "-cq", "14", "-profile:v", "high",
        "-c:a", "copy",
        str(out_path),
    ]
    vspipe_cmd = [VSPIPE, str(script_path), "-", "-c", "y4m"]

    run_piped_logged(job_id, STAGE, vspipe_cmd, ffmpeg_cmd)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("deinterlace stage produced no output file")

    height = settings.get("height", 0)
    if scan_type == "interlaced":
        tagged_height = height  # bob preserves frame height, just doubles frame rate
    else:
        tagged_height = height
    new_name = naming.set_progressive_tag(job["original_filename"], tagged_height)

    db.log(job_id, STAGE, f"deinterlace complete -> {out_path}")
    db.update_job(job_id, stage=STAGE, status="pending", current_file=str(out_path))
    db.merge_settings(job_id, {"display_filename": new_name})
