"""Stage 2: deinterlace / inverse-telecine to a progressive mezzanine file.

Reuses Hybrid's own bundled, fully portable VapourSynth + QTGMC/VIVTC engine
(see config.json's tools.vspipe / vs_plugins) so the output matches what the
guide's Hybrid-GUI workflow produces, driven headlessly via VSPipe piped into
ffmpeg. Also folds in PAR-to-square-pixels normalization and autodetected
letterbox/pillarbox crop from Stage 1, per the "combine with an earlier step"
allowance -- native aspect ratio is preserved, no forced 16:9 padding.

Independent of the `skip_upscale` (Upscale toggle) flag -- each toggle in the
processing stack controls only its own stage now (denoise/dehalo/upscale no
longer cascade off of each other). Final filename tagging and metadata
embedding both happen unconditionally in finalize.py, not here -- this stage
just produces a plain progressive .mp4 mezzanine and records a summary in
settings for finalize.py to read back later.

When the job's `deinterlace_enabled` flag is unset, this stage is a no-op
passthrough (same pattern as denoise/dehalo/upscale's own enabled checks) --
the source file is used as-is by whatever stage runs next.
"""
from __future__ import annotations

from pathlib import Path

from .. import db, naming
from ..config import CONFIG
from ..decoder_util import cuvid_decoder_args
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


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    settings = db.get_settings(job_id)
    src = Path(job["current_file"])

    if not job["deinterlace_enabled"]:
        db.log(job_id, STAGE, "deinterlace disabled for this job -- passing through unchanged")
        db.update_job(job_id, stage=STAGE, status="pending")
        db.merge_settings(job_id, {"deinterlace_summary": "skipped"})
        return

    scan_type = settings.get("scan_type", "interlaced")
    height = settings.get("height", 0)

    # Fast path: source already progressive, nothing to crop/PAR-normalize --
    # a genuine remux (stream copy), not a re-encode through QTGMC/VIVTC.
    if scan_type == "progressive" and _no_normalize_needed(settings):
        out_path = src.with_name(src.stem + "_deint.mp4")
        summary = "already progressive, no crop/PAR change needed -- stream copy, no re-encode"
        db.log(job_id, STAGE, summary)
        cmd = [
            FFMPEG, "-hide_banner", "-y", *cuvid_decoder_args(src), "-i", str(src),
            "-c", "copy",
            str(out_path),
        ]
        run_logged(job_id, STAGE, cmd)
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("deinterlace stage (stream copy) produced no output file")
        new_name = naming.set_progressive_tag(job["original_filename"], height)
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
        # src is opened again here purely for its audio track (-map 1:a:0?
        # below) -- still needs the cuvid decoder forced explicitly, same
        # QSV-autopick gotcha as everywhere else Topaz's ffmpeg touches a
        # real video codec (see decoder_util.py).
        *cuvid_decoder_args(src), "-i", str(src),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr_hq", "-cq", "14", "-profile:v", "high",
        "-c:a", "copy",
        str(out_path),
    ]

    vspipe_cmd = [VSPIPE, str(script_path), "-", "-c", "y4m"]

    run_piped_logged(job_id, STAGE, vspipe_cmd, ffmpeg_cmd)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("deinterlace stage produced no output file")

    new_name = naming.set_progressive_tag(job["original_filename"], height)

    db.log(job_id, STAGE, f"deinterlace complete -> {out_path}")
    db.update_job(job_id, stage=STAGE, status="pending", current_file=str(out_path))
    db.merge_settings(job_id, {"display_filename": new_name, "deinterlace_summary": deinterlace_summary})
