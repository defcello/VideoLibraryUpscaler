"""Stage (optional): Topaz Video AI "Dehalo" pre-clean pass -- two chained
tvai_up models run at 1:1 (no resolution change) between denoise and upscale,
to remove ringing/halo artifacts from oversharpened DVD sources. Driven via
the same headless ffmpeg + tvai_up approach as upscale.py.

The model shortnames and parameters (app/presets/dehalo.json) are NOT read off
the GUI's slider labels -- "Iris"/"Artemis"/"Medium quality"/"Strong Halo"
aren't ffmpeg-filter concepts. They were reverse-engineered by capturing the
real ffmpeg.exe command line Topaz Video AI itself ran for this exact GUI
stack (see dehalo.json's description/_note_recover_detail for what that
capture actually showed, including the surprising bit that "Recover detail =
100" does NOT show up as a literal `details` value in the real command).

Independent of the `skip_upscale` (Upscale toggle) flag -- each toggle in the
processing stack controls only its own stage now. Final filename tagging and
metadata embedding both happen unconditionally in finalize.py, not here.
Early-returns as a no-op passthrough when `job.dehalo_enabled` is unset.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .. import db, naming
from ..config import CONFIG, load_preset
from ..decoder_util import cuvid_decoder_args
from ..procutil import run_logged

STAGE = "dehaloed"

FFMPEG = CONFIG["tools"]["ffmpeg"]
FFPROBE = CONFIG["tools"]["ffprobe"]


def _current_dims(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        [FFPROBE, *cuvid_decoder_args(path), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    data = json.loads(proc.stdout)
    s = data["streams"][0]
    return int(s["width"]), int(s["height"])


def _tvai_params(params: dict) -> str:
    return ":".join(f"{k}={v}" for k, v in params.items())


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    settings = db.get_settings(job_id)
    src = Path(job["current_file"])

    if not job["dehalo_enabled"]:
        db.log(job_id, STAGE, "dehalo disabled for this job -- passing through unchanged")
        db.update_job(job_id, stage=STAGE, status="pending")
        db.merge_settings(job_id, {"dehalo_summary": "skipped"})
        return

    preset = load_preset("dehalo")
    width, height = _current_dims(src)

    filters = []
    for p in preset["passes"]:
        params = {"model": p["model"], **p["params"]}
        # scale=0 is the captured command line's way of saying "no AI scale
        # factor, target these exact dimensions instead" (see alqs-2 in
        # dehalo.json) -- scale=1 passes (e.g. iris-2) don't take w/h at all
        # in the real captured command.
        if params.get("scale") == 0:
            params["w"] = width
            params["h"] = height
        filters.append(f"tvai_up={_tvai_params(params)}")
    filters.append(f"scale=w={width}:h={height}:flags={preset['resize_flags']}")
    filters.append("format=yuv420p")
    filter_chain = ",".join(filters)

    pass_summary = " -> ".join(
        f"{p['model_display_name']} ({p['model']}, {p['input_condition_label']})" for p in preset["passes"]
    )
    db.log(job_id, STAGE, f"dehalo chain: {pass_summary} @ {width}x{height} (no resolution change)")

    out_path = src.with_name(src.stem + "_dehalo.mp4")
    enc = preset["encoder"]
    cmd = [
        FFMPEG, "-hide_banner", "-y",
        *cuvid_decoder_args(src), "-i", str(src),
        "-filter:v", filter_chain,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", enc["codec"], "-preset", enc["preset"], "-rc", enc["bitrate_mode"],
        "-cq", str(enc["cq"]), "-profile:v", enc["profile"],
        "-c:a", "copy",
        str(out_path),
    ]
    run_logged(job_id, STAGE, cmd, extra_env={"TVAI_MODEL_DIR": CONFIG["tvai_model_dir"]})

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("dehalo stage produced no output file")

    display_name = settings.get("display_filename") or job["original_filename"]
    new_name = naming.add_dehalo_tag(display_name)

    db.log(job_id, STAGE, f"dehalo complete -> {out_path}")
    db.update_job(job_id, stage=STAGE, status="pending", current_file=str(out_path))
    db.merge_settings(job_id, {"display_filename": new_name, "dehalo_summary": pass_summary})
