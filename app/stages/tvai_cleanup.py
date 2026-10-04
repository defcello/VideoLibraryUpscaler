"""Shared runner for the 1:1 (no resolution change) Topaz Video AI clean-up
stages -- deblur.py (Iris) and dehalo.py (Artemis Strong Halo). Each one is a
preset of chained tvai_up passes driven through Topaz's own bundled ffmpeg,
same headless approach as upscale.py.

The model shortnames and parameters in the presets (app/presets/deblur.json,
dehalo.json) are NOT read off the GUI's slider labels -- they were
reverse-engineered by capturing the real ffmpeg.exe command line Topaz Video AI
itself ran for the original combined Iris -> Artemis GUI stack, then split into
one preset per stage so blurry-but-halo-free sources can be cleaned up without
also running the dehalo model.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from .. import db
from ..config import CONFIG, load_preset
from ..decoder_util import cuvid_decoder_args
from ..procutil import ffmpeg_time_progress, run_logged

FFMPEG = CONFIG["tools"]["ffmpeg"]
FFPROBE = CONFIG["tools"]["ffprobe"]


def _current_dims(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        [FFPROBE, *cuvid_decoder_args(path), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not path.exists():
        raise RuntimeError(f"ffprobe failed to read dimensions from {path}: {proc.stderr[-2000:]}")
    s = json.loads(proc.stdout)["streams"][0]
    return int(s["width"]), int(s["height"])


def _tvai_params(params: dict) -> str:
    return ":".join(f"{k}={v}" for k, v in params.items())


def resolve_passes(preset: dict, height: int) -> list[dict]:
    """Returns the preset's passes with any height-dependent `variants`
    (e.g. Iris Low vs Medium quality in deblur.json) resolved to a concrete
    model/input_condition_label for an input of `height` pixels."""
    out = []
    for p in preset["passes"]:
        if "variants" in p:
            v = next(v for v in p["variants"] if height >= v["min_height"])
            p = {**p, "model": v["model"], "input_condition_label": v["input_condition_label"]}
        out.append(p)
    return out


def pass_summary(passes: list[dict]) -> str:
    return " -> ".join(
        f"{p['model_display_name']} ({p['model']}, {p['input_condition_label']})" for p in passes
    )


def run_cleanup(
    job_id: str,
    *,
    stage: str,
    name: str,
    enabled: bool,
    preset_name: str,
    tag_fn: Callable[[str], str],
) -> None:
    """`name` is the short lowercase word used in logs, the output file
    suffix and the `<name>_summary` settings key (e.g. "deblur")."""
    job = db.get_job(job_id)
    settings = db.get_settings(job_id)
    src = Path(job["current_file"])
    summary_key = f"{name}_summary"

    if not enabled:
        db.log(job_id, stage, f"{name} disabled for this job -- passing through unchanged")
        db.update_job(job_id, stage=stage, status="pending")
        db.merge_settings(job_id, {summary_key: "skipped"})
        return

    preset = load_preset(preset_name)
    width, height = _current_dims(src)

    passes = resolve_passes(preset, height)
    filters = []
    for p in passes:
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

    summary = pass_summary(passes)
    db.log(job_id, stage, f"{name} chain: {summary} @ {width}x{height} (no resolution change)")

    out_path = src.with_name(src.stem + f"_{name}.mp4")
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
    run_logged(job_id, stage, cmd, extra_env={"TVAI_MODEL_DIR": CONFIG["tvai_model_dir"]},
               progress=ffmpeg_time_progress(settings.get("duration")))

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"{name} stage produced no output file")

    display_name = settings.get("display_filename") or job["original_filename"]
    new_name = tag_fn(display_name)

    db.log(job_id, stage, f"{name} complete -> {out_path}")
    db.update_job(job_id, stage=stage, status="pending", current_file=str(out_path))
    db.merge_settings(job_id, {"display_filename": new_name, summary_key: summary})
