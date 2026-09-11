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
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
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

    preset = load_preset(job["topaz_preset"] or CONFIG["default_topaz_preset"])

    width, height = _current_dims(src)
    target_h = preset["output_tier_height"]
    target_w = int(round(width * target_h / height / 2) * 2)
    db.log(job_id, STAGE, f"source {width}x{height} -> target {target_w}x{target_h} (model={preset['model']})")

    filters = []
    if preset.get("precleanup", {}).get("enabled"):
        pc = preset["precleanup"]
        pc_params = {"model": pc["model"], "scale": 1, **pc["params"]}
        filters.append(f"tvai_up={_tvai_params(pc_params)}")
        db.log(job_id, STAGE, f"precleanup enabled: model={pc['model']}")

    up_params = {"model": preset["model"], "w": target_w, "h": target_h, **preset["tvai_up_params"]}
    filters.append(f"tvai_up={_tvai_params(up_params)}")
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
