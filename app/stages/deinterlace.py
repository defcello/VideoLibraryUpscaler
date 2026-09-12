"""Stage 2: deinterlace / inverse-telecine to a progressive mezzanine file.

Reuses Hybrid's own bundled, fully portable VapourSynth + QTGMC/VIVTC engine
(see config.json's tools.vspipe / vs_plugins) so the output matches what the
guide's Hybrid-GUI workflow produces, driven headlessly via VSPipe piped into
ffmpeg. Also folds in PAR-to-square-pixels normalization and autodetected
letterbox/pillarbox crop from Stage 1, per the "combine with an earlier step"
allowance -- native aspect ratio is preserved, no forced 16:9 padding.

When the job's `skip_upscale` flag is set, this becomes the TERMINAL stage
(denoise and upscale both pass through unchanged -- see those stages' early
returns): the output here gets the final filename tag and embedded metadata
that upscale.py would otherwise be responsible for.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .. import db, naming
from ..config import CONFIG
from ..metadata_tags import ffmpeg_metadata_args, processed_date
from ..procutil import run_logged, run_piped_logged
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


def _no_normalize_needed(settings: dict) -> bool:
    crop = (settings.get("crop_left", 0), settings.get("crop_right", 0),
            settings.get("crop_top", 0), settings.get("crop_bottom", 0))
    par = (settings.get("par_num", 1), settings.get("par_den", 1))
    return crop == (0, 0, 0, 0) and par[0] == par[1]


def _scan_summary(scan_type: str, settings: dict) -> str:
    conf = settings.get("confidence")
    conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "n/a"
    tff = settings.get("tff")
    return f"{scan_type} (tff={tff}, confidence={conf_str})"


def _build_terminal_metadata(job: dict, settings: dict, deinterlace_summary: str) -> dict:
    return {
        "TOOL": "AI Remaster Pipeline",
        "SOURCE_FILE": job["original_filename"],
        "SCAN_DETECTION": _scan_summary(settings.get("scan_type", "?"), settings),
        "DEINTERLACE": deinterlace_summary,
        "DENOISE": "skipped (skip_upscale mode)",
        "UPSCALE": "skipped (skip_upscale mode)",
        "PROCESSED": processed_date(),
    }


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    settings = db.get_settings(job_id)
    src = Path(job["current_file"])
    scan_type = settings.get("scan_type", "interlaced")
    skip_upscale = bool(job["skip_upscale"])
    height = settings.get("height", 0)

    # Fast path: skip-upscale mode, source already progressive, nothing to
    # crop/PAR-normalize -- a genuine remux (stream copy), not a re-encode.
    # Still routed through ffmpeg rather than a raw byte copy so we can tag
    # the file with the processing metadata below.
    if skip_upscale and scan_type == "progressive" and _no_normalize_needed(settings):
        out_path = src.with_name(src.stem + "_deint" + src.suffix)
        summary = "already progressive, no crop/PAR change needed -- stream copy, no re-encode"
        db.log(job_id, STAGE, f"skip_upscale: {summary}")
        meta = _build_terminal_metadata(job, settings, summary)
        cmd = [
            FFMPEG, "-hide_banner", "-y", "-i", str(src),
            "-c", "copy", *ffmpeg_metadata_args(meta),
            str(out_path),
        ]
        run_logged(job_id, STAGE, cmd)
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("deinterlace stage (stream copy) produced no output file")
        new_name = naming.set_final_progressive_tag(job["original_filename"], height)
        db.log(job_id, STAGE, f"deinterlace complete (stream copy) -> {out_path}")
        db.update_job(job_id, stage=STAGE, status="pending", current_file=str(out_path))
        db.merge_settings(job_id, {"display_filename": new_name, "deinterlace_summary": summary})
        return

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

    if scan_type == "interlaced":
        deinterlace_summary = "QTGMC Very Slow (Bob, FPSDivisor=1 -- full field-rate output, TFF=" + str(settings.get("tff")) + ")"
    elif scan_type == "telecine":
        deinterlace_summary = "VIVTC IVTC (VFM mode=1 + VDecimate -> 23.976p, TFF=" + str(settings.get("tff")) + ")"
    else:
        deinterlace_summary = "already progressive; PAR/crop normalize only"

    out_path = src.with_name(src.stem + "_deint.mp4")

    ffmpeg_cmd = [
        FFMPEG, "-hide_banner", "-y",
        "-f", "yuv4mpegpipe", "-i", "-",
        "-i", str(src),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr_hq", "-cq", "14", "-profile:v", "high",
        "-c:a", "copy",
    ]
    if skip_upscale:
        meta = _build_terminal_metadata(job, settings, deinterlace_summary)
        ffmpeg_cmd += ffmpeg_metadata_args(meta)
    ffmpeg_cmd.append(str(out_path))

    vspipe_cmd = [VSPIPE, str(script_path), "-", "-c", "y4m"]

    run_piped_logged(job_id, STAGE, vspipe_cmd, ffmpeg_cmd)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("deinterlace stage produced no output file")

    if skip_upscale:
        new_name = naming.set_final_progressive_tag(job["original_filename"], height)
    else:
        new_name = naming.set_progressive_tag(job["original_filename"], height)

    db.log(job_id, STAGE, f"deinterlace complete -> {out_path}")
    db.update_job(job_id, stage=STAGE, status="pending", current_file=str(out_path))
    db.merge_settings(job_id, {"display_filename": new_name, "deinterlace_summary": deinterlace_summary})
