"""Stage 4: Topaz Video AI upscale, driven headlessly via Topaz's own bundled
ffmpeg.exe and its `tvai_up` filter (confirmed present: `ffmpeg -h
filter=tvai_up` exposes every parameter the Topaz GUI has). Preset-driven --
see app/presets/topaz_default.json -- since this is the piece you'll most
want to keep tuning per title/series.

Model shortnames were confirmed against this machine's installed model JSONs
(C:\\ProgramData\\Topaz Labs LLC\\Topaz Video\\models\\*.json):
  gcg-5 = "Gaia - Computer Generated" (the guide's "Gaia CG" / "Input Video
          Type: Computer Generated") -- Precision-class, not Generative.
  nyx-3 = "Nyx" pre-clean denoise, for heavily degraded sources.
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from .. import db, naming
from ..config import CONFIG, load_preset
from ..decoder_util import cuvid_decoder_args
from ..procutil import run_logged

STAGE = "upscaled"

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


def _available_scales(model: str) -> list[int]:
    """Each Topaz model only supports specific integer scales (e.g. gcg-5 is
    1/2/4, not 3 -- confirmed by testing: ffmpeg rejects "Invalid scale 3 for
    model gcg-5, allowed scales are: 1, 2, 4"). Read the model's own JSON
    (same files used to confirm model shortnames) rather than hardcoding a set
    that only fits one model."""
    path = Path(CONFIG["tvai_model_dir"]) / f"{model}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for backend in data.get("backends", {}).values():
            scales = backend.get("scales")
            if scales:
                return sorted(int(s) for s in scales.keys())
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return [1, 2, 3, 4]  # fallback if the model json is missing/unreadable


def _build_scale_passes(source_h: int, target_h: int, model: str) -> list[int]:
    """tvai_up's `scale` is a coarse integer AI upscale factor -- NOT the same
    thing as its `w`/`h` params, which are only "estimate" hints used for
    auto-picking a scale when `estimate` sampling is enabled, and do nothing
    to the actual output size on their own (confirmed by testing: passing w/h
    alone left output at the source resolution, scale=1 default).

    Returns the list of scale factors to run tvai_up with IN SEQUENCE. If a
    single available scale covers the requested tier, that's one pass (e.g.
    gcg-5 covers a 2.25x need with one scale=4 pass). If the model's largest
    scale doesn't cover it alone (e.g. ganim-1 only offers 2x), we chain that
    largest scale repeatedly -- two 2x AI passes for a ~4x need -- rather
    than falling back to a plain (non-AI) resize for the remainder. A final
    `scale=` filter still resizes to the exact target dimensions afterward."""
    options = _available_scales(model)
    if target_h <= source_h:
        return [1] if 1 in options else []

    needed = target_h / source_h
    covering = [s for s in options if s >= needed]
    if covering:
        return [min(covering)]

    largest = max(options)
    passes: list[int] = []
    cumulative = 1.0
    while cumulative < needed and len(passes) < 3:
        passes.append(largest)
        cumulative *= largest
    return passes


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    settings = db.get_settings(job_id)
    src = Path(job["current_file"])

    preset = load_preset(job["topaz_preset"] or CONFIG["default_topaz_preset"])

    width, height = _current_dims(src)
    target_h = preset["output_tier_height"]
    target_w = int(round(width * target_h / height / 2) * 2)
    scale_passes = _build_scale_passes(height, target_h, preset["model"])
    db.log(job_id, STAGE, f"source {width}x{height} -> target {target_w}x{target_h} "
                          f"(model={preset['model']}, scale passes={scale_passes or 'none'}, then exact resize)")

    filters = []
    if preset.get("precleanup", {}).get("enabled"):
        pc = preset["precleanup"]
        pc_params = {"model": pc["model"], "scale": 1, **pc["params"]}
        filters.append(f"tvai_up={_tvai_params(pc_params)}")
        db.log(job_id, STAGE, f"precleanup enabled: model={pc['model']}")

    for s in scale_passes:
        up_params = {"model": preset["model"], "scale": s, **preset["tvai_up_params"]}
        filters.append(f"tvai_up={_tvai_params(up_params)}")
    # tvai_up's scale is a coarse integer AI factor -- resize precisely to the
    # requested tier afterward. lanczos rings/haloes on hard edges (a likely
    # contributor to edge ghosting on flat-color animation), so presets
    # default to bicubic; still overridable per preset if wanted.
    resize_flags = preset.get("resize_flags", "bicubic")
    filters.append(f"scale={target_w}:{target_h}:flags={resize_flags}")
    # tvai_up's internal pipeline works in higher bit depth (rgb48le/etc);
    # convert back to standard 8-bit before h264_nvenc, which errors ("10 bit
    # encode not supported") if fed that directly.
    filters.append("format=yuv420p")
    filter_chain = ",".join(filters)

    out_path = src.with_name(src.stem + "_upscaled." + preset["encoder"]["container"])

    decoder_args = cuvid_decoder_args(src)
    cmd = [
        FFMPEG, "-hide_banner", "-y",
        *decoder_args,
        "-i", str(src),
        "-filter:v", filter_chain,
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", preset["encoder"]["codec"],
        "-preset", preset["encoder"]["preset"],
        "-rc", preset["encoder"]["bitrate_mode"],
        "-cq", str(preset["encoder"]["cq"]),
        "-profile:v", preset["encoder"]["profile"],
        "-c:a", preset["encoder"]["audio_mode"],
        str(out_path),
    ]
    run_logged(job_id, STAGE, cmd, extra_env={"TVAI_MODEL_DIR": CONFIG["tvai_model_dir"]})

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("upscale stage produced no output file")

    display_name = settings.get("display_filename") or job["original_filename"]
    new_name = naming.set_upscaled_tag(display_name, target_h)

    db.log(job_id, STAGE, f"upscale complete -> {out_path}")
    db.update_job(job_id, stage=STAGE, status="pending", current_file=str(out_path))
    db.merge_settings(job_id, {"display_filename": new_name, "upscaled_width": target_w, "upscaled_height": target_h})
