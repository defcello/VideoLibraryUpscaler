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

If the job's `skip_upscale` flag is set, this stage is a no-op -- independent
of the other toggles now, unlike denoise/dehalo it doesn't cascade off of
anything else. Final filename tagging and metadata embedding both happen
unconditionally in finalize.py, not here.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path
from typing import Optional

from .. import db, naming
from ..config import CONFIG, load_preset
from ..decoder_util import cuvid_decoder_args
from ..procutil import CommandError, ffmpeg_time_progress, run_logged
from ..topaz_models import build_scale_passes

STAGE = "upscaled"

FFMPEG = CONFIG["tools"]["ffmpeg"]
FFPROBE = CONFIG["tools"]["ffprobe"]
NEUROSERVER = CONFIG["tools"].get("neuroserver")


def _current_dims(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        [FFPROBE, *cuvid_decoder_args(path), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    data = json.loads(proc.stdout)
    s = data["streams"][0]
    return int(s["width"]), int(s["height"])


def _current_duration(path: Path) -> float:
    proc = subprocess.run(
        [FFPROBE, *cuvid_decoder_args(path), "-v", "error",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return float(json.loads(proc.stdout)["format"]["duration"])


def _tvai_params(params: dict) -> str:
    return ":".join(f"{k}={v}" for k, v in params.items())


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    settings = db.get_settings(job_id)
    src = Path(job["current_file"])

    if job["skip_upscale"]:
        db.log(job_id, STAGE, "skip_upscale set -- passing through unchanged")
        db.update_job(job_id, stage=STAGE, status="pending")
        return

    preset = load_preset(job["topaz_preset"] or CONFIG["default_topaz_preset"])

    if preset.get("engine") == "neuroserver":
        _run_generative(job_id, job, settings, preset, src)
        return

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
    run_logged(job_id, STAGE, cmd, extra_env={"TVAI_MODEL_DIR": CONFIG["tvai_model_dir"]},
               progress=ffmpeg_time_progress(settings.get("duration")))

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("upscale stage produced no output file")

    display_name = settings.get("display_filename") or job["original_filename"]
    new_name = naming.set_upscaled_tag(display_name, target_h)

    db.log(job_id, STAGE, f"upscale complete -> {out_path}")
    db.update_job(job_id, stage=STAGE, status="pending", current_file=str(out_path))
    db.merge_settings(job_id, {
        "display_filename": new_name,
        "upscaled_width": target_w, "upscaled_height": target_h,
        "upscale_summary": upscale_summary,
    })


def _find_recovered_output(requested_out: Path) -> Optional[Path]:
    """neuroserver.exe's internal post-process step (a second tvai_up pass
    with the Nyx-3 denoise model, meant to AI-upscale the raw diffusion
    output the rest of the way to the exact requested tier) reliably fails on
    this machine with ffmpeg's h264_qsv 'Error creating a MFX session' -- see
    topaz_film_generative.json's _note_post_process_bug. When that happens,
    the requested output path itself is missing/empty, but the raw,
    pre-post-process diffusion output survives as a
    '<requested_out_name>.temp.<hash>.mp4' sibling file, which is complete
    and valid (just missing audio, which Topaz strips before the
    post-process step) -- the user independently discovered and already
    relies on this exact recovery in the real GUI."""
    if requested_out.exists() and requested_out.stat().st_size > 0:
        return requested_out
    for candidate in sorted(requested_out.parent.glob(requested_out.name + ".temp.*.mp4")):
        if candidate.stat().st_size > 0:
            return candidate
    return None


def _run_generative(job_id: str, job, settings: dict, preset: dict, src: Path) -> None:
    """Generative (Starlight) upscale, driven via neuroserver.exe directly --
    these models aren't reachable through ffmpeg's tvai_up filter at all
    (confirmed: 'astrasharp' etc. are absent from tvai_up's compiled model
    enum on this machine). See topaz_film_generative.json for the model
    choice and the known post-process bug this works around."""
    if not NEUROSERVER:
        raise RuntimeError("config.json is missing tools.neuroserver -- required for the generative engine")

    width, height = _current_dims(src)
    needed = preset["output_tier_height"] / height
    # Single-pass, smallest integer scale that covers the tier -- mirrors the
    # classic engine's covering-scale logic (see topaz_models.build_scale_passes)
    # rather than chaining passes. The model's real output height can land
    # below this nominal request (see _note_output_tier in the preset) -- the
    # actual recovered file is measured and validated below, not assumed.
    nominal_scale = max(1, math.ceil(needed))
    target_w = int(round(width * nominal_scale / 2) * 2)
    target_h = int(round(height * nominal_scale / 2) * 2)

    db.log(job_id, STAGE, f"generative source {width}x{height} -> nominal {nominal_scale}x request "
                          f"({target_w}x{target_h}), model={preset['model']}")

    requested_out = src.with_name(src.stem + "_upscaled_raw.mp4")
    cmd = [
        NEUROSERVER, "--once",
        "--input-path", str(src),
        "--output-path", str(requested_out),
        "--input-width", str(width),
        "--input-height", str(height),
        "--output-width", str(target_w),
        "--output-height", str(target_h),
        "--upscale-factor", str(nominal_scale),
        "--max-gpu-mem", str(preset["max_gpu_mem_gb"]),
        "--filters", json.dumps([{"model": preset["model"]}]),
        "--ffmpeg-encoding", preset["ffmpeg_encoding"],
    ]
    # neuroserver needs TOPAZ_MODEL_STORE explicitly when run standalone (same
    # reasoning as TVAI_MODEL_DIR elsewhere), and its own internal ffmpeg call
    # for the post-process step resolves a bare "ffmpeg" off PATH -- without
    # Topaz's ffmpeg directory prepended, PATH resolves to a different, non-tvai
    # ffmpeg build on this machine and that internal call fails outright with
    # "Filter not found" instead of the (recoverable) QSV decoder error.
    extra_env = {
        "TOPAZ_MODEL_STORE": CONFIG["topaz_model_store"],
        "PATH": f"{Path(FFMPEG).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        # The diffusion pass processes the source in sequential ~5s segments
        # and (confirmed by testing: a 20s clip OOM'd on its 3rd segment,
        # "13.06 GiB allocated by PyTorch... 2.36 GiB reserved but
        # unallocated") does not fully release memory between them -- usage
        # climbs across the job rather than staying flat. expandable_segments
        # is PyTorch's own suggested mitigation for this fragmentation
        # pattern (it was named directly in the CUDA OOM error text); it
        # reduces but may not eliminate the growth, so the duration check
        # below is the real safety net, not this.
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
    }
    try:
        # neuroserver.exe resolves its own Lib/site-packages (torch, etc.)
        # relative to the current working directory, not its own exe path --
        # without cwd set here it inherits this server process's cwd instead
        # and fails with "ModuleNotFoundError: No module named 'torch'".
        run_logged(job_id, STAGE, cmd, cwd=Path(NEUROSERVER).parent, extra_env=extra_env)
    except CommandError:
        db.log(job_id, STAGE, "neuroserver exited non-zero -- checking for the known recoverable "
                              "post-process-step failure before treating this as a real failure")

    recovered = _find_recovered_output(requested_out)
    if recovered is None:
        raise RuntimeError("generative upscale produced no usable output (no recoverable .temp.*.mp4 file either)")

    out_w, out_h = _current_dims(recovered)
    if out_h < preset["output_tier_height"]:
        raise RuntimeError(f"generative upscale result is only {out_w}x{out_h}, below the "
                           f"{preset['output_tier_height']}p minimum tier")

    # The recovered .temp.*.mp4 file existing is NOT proof the diffusion pass
    # actually finished -- confirmed by testing: the same file shape (present,
    # valid header, missing audio) results from BOTH the known harmless
    # post-process bug (full-length, complete) AND a genuine mid-job CUDA OOM
    # crash (silently truncated to whatever had been produced so far). A
    # duration check is the only way to tell them apart.
    src_duration = _current_duration(src)
    out_duration = _current_duration(recovered)
    tolerance = max(2.0, src_duration * 0.1)
    if out_duration < src_duration - tolerance:
        raise RuntimeError(
            f"generative upscale result is only {out_duration:.1f}s of an expected {src_duration:.1f}s -- "
            f"likely a mid-job crash (e.g. CUDA OOM), not the known harmless post-process-step failure"
        )
    db.log(job_id, STAGE, f"recovered generative output: {recovered} ({out_w}x{out_h}, {out_duration:.1f}s)")

    out_path = src.with_name(src.stem + "_upscaled." + preset["encoder"]["container"])
    upscale_summary = (f"Topaz {preset['model_display_name']} ({preset['model']}), "
                       f"generative single-pass, requested {nominal_scale}x, result={out_w}x{out_h}")
    # The recovered file has no audio (Topaz strips it before the failing
    # post-process step) -- remux the source's audio back in via a plain
    # stream copy (no re-encode, no decoder concerns either way).
    cmd = [
        FFMPEG, "-hide_banner", "-y",
        "-i", str(recovered),
        "-i", str(src),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c", "copy",
        str(out_path),
    ]
    db.log(job_id, STAGE, "muxing original audio back into the recovered generative output")
    run_logged(job_id, STAGE, cmd)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("audio remux for generative upscale produced no output file")

    if recovered != out_path:
        recovered.unlink(missing_ok=True)
    if requested_out.exists() and requested_out != recovered:
        requested_out.unlink(missing_ok=True)

    display_name = settings.get("display_filename") or job["original_filename"]
    new_name = naming.set_upscaled_tag(display_name, out_h)

    db.log(job_id, STAGE, f"generative upscale complete -> {out_path}")
    db.update_job(job_id, stage=STAGE, status="pending", current_file=str(out_path))
    db.merge_settings(job_id, {
        "display_filename": new_name,
        "upscaled_width": out_w, "upscaled_height": out_h,
        "upscale_summary": upscale_summary,
    })
