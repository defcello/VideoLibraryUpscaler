"""Stage 4: Topaz Video AI upscale, driven headlessly via Topaz's own bundled
ffmpeg.exe and its `tvai_up` filter (confirmed present: `ffmpeg -h
filter=tvai_up` exposes every parameter the Topaz GUI has). Preset-driven --
see app/presets/topaz_film.json / topaz_animation.json -- since this is the
piece you'll most want to keep tuning per title/series.

Model shortnames were confirmed against this machine's installed model JSONs
(C:\\ProgramData\\Topaz Labs LLC\\Topaz Video\\models\\*.json):
  gcg-5   = "Gaia - Computer Generated" (Film preset) -- Precision-class.
  ganim-1 = "Gaia Animation" (Animation preset) -- trained for flat-color
            cel/hand-drawn content; only supports a fixed 2x per pass, so
            reaching a 4x-ish tier chains two passes (see topaz_models.py).
  nyx-3   = "Nyx" pre-clean denoise, for heavily degraded sources.

If the job's `skip_upscale` flag is set, this stage is a no-op (see
deinterlace.py, which becomes the terminal stage in that mode instead).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .. import db, naming
from ..config import CONFIG, load_preset
from ..decoder_util import cuvid_decoder_args
from ..metadata_tags import ffmpeg_metadata_args, processed_date
from ..procutil import run_logged
from ..topaz_models import build_scale_passes

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


def _scan_summary(settings: dict) -> str:
    conf = settings.get("confidence")
    conf_str = f"{conf:.2f}" if isinstance(conf, (int, float)) else "n/a"
    return f"{settings.get('scan_type', '?')} (tff={settings.get('tff')}, confidence={conf_str})"


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    settings = db.get_settings(job_id)
    src = Path(job["current_file"])

    if job["skip_upscale"]:
        db.log(job_id, STAGE, "skip_upscale set -- passing through unchanged (deinterlace.py is the terminal stage)")
        db.update_job(job_id, stage=STAGE, status="pending")
        return

    preset = load_preset(job["topaz_preset"] or CONFIG["default_topaz_preset"])

    width, height = _current_dims(src)
    target_h = preset["output_tier_height"]
    target_w = int(round(width * target_h / height / 2) * 2)
    scale_passes = build_scale_passes(height, target_h, preset["model"])
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

    upscale_summary = (f"Topaz {preset['model_display_name']} ({preset['model']}), "
                       f"passes={scale_passes or 'none'}, target={target_w}x{target_h}, "
                       f"resize={resize_flags}")
    meta = {
        "TOOL": "AI Remaster Pipeline",
        "SOURCE_FILE": job["original_filename"],
        "SCAN_DETECTION": _scan_summary(settings),
        "DEINTERLACE": settings.get("deinterlace_summary", "n/a"),
        "DENOISE": settings.get("denoise_summary", "skipped"),
        "UPSCALE": upscale_summary,
        "ENCODER": f"{preset['encoder']['codec']} {preset['encoder']['profile']} cq={preset['encoder']['cq']}",
        "PROCESSED": processed_date(),
    }

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
        *ffmpeg_metadata_args(meta),
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
