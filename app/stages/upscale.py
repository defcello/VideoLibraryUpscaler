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
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .. import db, naming, procutil, vram_probe
from ..config import CONFIG, load_preset
from ..decoder_util import cuvid_decoder_args
from ..procutil import CommandError, ProgressParser, ffmpeg_time_progress, run_logged
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
    if proc.returncode != 0 or not path.exists():
        raise RuntimeError(f"ffprobe failed to read dimensions from {path}: {proc.stderr[-2000:]}")
    s = json.loads(proc.stdout)["streams"][0]
    return int(s["width"]), int(s["height"])


def _current_duration(path: Path) -> float:
    proc = subprocess.run(
        [FFPROBE, *cuvid_decoder_args(path), "-v", "error",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not path.exists():
        raise RuntimeError(f"ffprobe failed to read duration from {path}: {proc.stderr[-2000:]}")
    return float(json.loads(proc.stdout)["format"]["duration"])


def _current_fps(path: Path) -> float:
    proc = subprocess.run(
        [FFPROBE, *cuvid_decoder_args(path), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not path.exists():
        raise RuntimeError(f"ffprobe failed to read fps from {path}: {proc.stderr[-2000:]}")
    num, den = json.loads(proc.stdout)["streams"][0]["r_frame_rate"].split("/")
    return float(num) / float(den)


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


_BLACKDETECT_RE = re.compile(r"black_start:([\d.]+)")
_CHUNK_SHAPE_RE = re.compile(r"torch\.Size\(\[3, (\d+),")


def _blackdetect_boundaries(job_id: str, src: Path, min_spacing: float) -> list[float]:
    """One-time pre-pass (result cached in settings_json, never recomputed):
    scans the whole pre-upscale source for black frames via ffmpeg's own
    `blackdetect` filter (decode-only, no encode, so it's cheap even on a
    long source) and keeps only the ones at least `min_spacing` seconds
    apart -- these become the only points a generative-upscale segment is
    ever allowed to end/start at, since a true black-to-black cut hides any
    seam between two independently-generated segments."""
    bd = CONFIG["blackdetect"]
    vf = f"blackdetect=d={bd['d']}:pic_th={bd['pic_th']}:pix_th={bd['pix_th']}"
    cmd = [FFMPEG, "-hide_banner", "-y", *cuvid_decoder_args(src), "-i", str(src),
           "-vf", vf, "-an", "-f", "null", "-"]
    db.log(job_id, STAGE, "scanning for black-frame checkpoint boundaries (one-time pre-pass)")
    output = run_logged(job_id, STAGE, cmd)
    candidates = sorted(float(m.group(1)) for m in _BLACKDETECT_RE.finditer(output))
    boundaries: list[float] = []
    for t in candidates:
        if not boundaries or t - boundaries[-1] >= min_spacing:
            boundaries.append(t)
    db.log(job_id, STAGE, f"found {len(boundaries)} usable checkpoint boundary(ies) "
                          f"(>= {min_spacing}s apart) out of {len(candidates)} black-frame detections")
    return boundaries


def _trim_segment(job_id: str, src: Path, start: float, end: Optional[float], tag: str) -> Path:
    """Cuts [start, end) out of src for one checkpoint segment. Unlike
    ingest.py's test-crop trim (input-side -ss, snaps to the nearest
    keyframe -- an accepted tradeoff for a disposable testing clip), this
    puts -ss AFTER -i for frame-accurate seeking: these boundaries are real
    delivered-master splice points, so a keyframe-snap overlap/gap between
    segments would be a visible defect, not just an testing inconvenience.
    Still -c copy for the kept span itself -- only the skipped prefix is
    actually decoded."""
    trimmed = src.with_name(src.stem + f"_{tag}" + src.suffix)
    cmd = [FFMPEG, "-hide_banner", "-y", *cuvid_decoder_args(src), "-i", str(src), "-ss", str(start)]
    if end is not None:
        cmd += ["-t", str(end - start)]
    cmd += ["-c", "copy", str(trimmed)]
    db.log(job_id, STAGE, f"trimming checkpoint segment [{start:.2f}, "
                          f"{end if end is not None else 'end'}) -> {trimmed}")
    run_logged(job_id, STAGE, cmd)
    if not trimmed.exists() or trimmed.stat().st_size == 0:
        raise RuntimeError("checkpoint segment trim produced no output file")
    return trimmed


def _generative_chunk_progress(seg_start: float, seg_duration: float, total_duration: float, fps: float) -> ProgressParser:
    """neuroserver's own progress signal is unusable (its "frame" counter
    isn't frame-accurate -- confirmed by testing: it reached 1,098,449 on a
    source with ~50,000 real frames -- and its "progress" field never moves
    past 5). What IS reliable: it logs one line per internal processing
    chunk boundary (`shape of the original video: torch.Size([3,
    <chunk_frames>, H, W])`, confirmed against real logs from a finished
    job). Counting those lines against the chunk size revealed by the first
    one gives a genuine, frame-accurate progress fraction for the segment
    currently running, which this then maps onto the whole job's timeline
    (segments already checkpointed count as done)."""
    state: dict = {"total_chunks": None, "chunks_seen": 0}

    def parse(line: str) -> Optional[float]:
        if not total_duration:
            return None
        m = _CHUNK_SHAPE_RE.search(line)
        if not m:
            return None
        chunk_frames = int(m.group(1))
        if state["total_chunks"] is None:
            total_seg_frames = max(1, round(seg_duration * fps))
            state["total_chunks"] = max(1, math.ceil(total_seg_frames / chunk_frames))
        state["chunks_seen"] += 1
        seg_frac = min(1.0, (state["chunks_seen"] - 1) / state["total_chunks"])
        overall_seconds = seg_start + seg_frac * seg_duration
        return max(0.0, min(99.0, overall_seconds / total_duration * 100))

    return parse


def _persist_input_if_volatile(job_id: str, src: Path, checkpoint_dir: Path) -> Path:
    """needs_restart specifically anticipates a REAL machine restart (VRAM
    fragmentation is documented as something only a reboot reliably clears),
    not just this server process restarting. On this machine jobs are
    staged on R:\\ (a RAM disk, wiped on reboot -- ingest.py already warns
    about this) far more often than the "almost always" comment in
    ingest.py assumes -- confirmed live: every currently in-progress job's
    staging_drive is R:\\. Without this, a real reboot would wipe the
    generative-upscale input out from under a needs_restart job, and it
    would fail loudly on resume instead of picking back up. So: whenever a
    job is about to be parked as needs_restart, make sure its current input
    file is copied into the same persistent checkpoint_dir the segment
    outputs already live in, and the DB's current_file updated to match."""
    if src.parent == checkpoint_dir:
        return src  # already persisted (e.g. a previous needs_restart already did this)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    persisted = checkpoint_dir / src.name
    if not persisted.exists():
        db.log(job_id, STAGE, f"copying generative-upscale input to persistent storage before "
                              f"parking as needs_restart (its staging location may not survive a "
                              f"real machine restart): {src} -> {persisted}")
        shutil.copyfile(src, persisted)
    db.update_job(job_id, current_file=str(persisted))
    return persisted


def _run_generative_segment(
    job_id: str, preset: dict, src: Path, seg_start: float, seg_end: Optional[float],
    seg_duration: float, total_duration: float, seg_index: int,
) -> tuple[Path, int, int]:
    """Runs neuroserver on exactly [seg_start, seg_end) of src (or the whole
    file if this is the only segment) and returns the validated, audio-less
    recovered output for just this span."""
    is_whole_file = seg_start == 0 and seg_end is None
    trimmed = src if is_whole_file else _trim_segment(job_id, src, seg_start, seg_end, f"seg{seg_index}")

    width, height = _current_dims(trimmed)
    needed = preset["output_tier_height"] / height
    # Single-pass, smallest integer scale that covers the tier -- mirrors the
    # classic engine's covering-scale logic (see topaz_models.build_scale_passes)
    # rather than chaining passes. The model's real output height can land
    # below this nominal request (see _note_output_tier in the preset) -- the
    # actual recovered file is measured and validated below, not assumed.
    nominal_scale = max(1, math.ceil(needed))
    target_w = int(round(width * nominal_scale / 2) * 2)
    target_h = int(round(height * nominal_scale / 2) * 2)

    db.log(job_id, STAGE, f"segment {seg_index} [{seg_start:.2f}s, "
                          f"{seg_end if seg_end is not None else total_duration:.2f}s): "
                          f"{width}x{height} -> nominal {nominal_scale}x request ({target_w}x{target_h}), "
                          f"model={preset['model']}")

    requested_out = trimmed.with_name(trimmed.stem + f"_upscaled_raw_seg{seg_index}.mp4")
    cmd = [
        NEUROSERVER, "--once",
        "--input-path", str(trimmed),
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
    fps = _current_fps(trimmed)
    progress = _generative_chunk_progress(seg_start, seg_duration, total_duration, fps)
    try:
        # neuroserver.exe resolves its own Lib/site-packages (torch, etc.)
        # relative to the current working directory, not its own exe path --
        # without cwd set here it inherits this server process's cwd instead
        # and fails with "ModuleNotFoundError: No module named 'torch'".
        run_logged(job_id, STAGE, cmd, cwd=Path(NEUROSERVER).parent, extra_env=extra_env, progress=progress)
    except CommandError:
        db.log(job_id, STAGE, "neuroserver exited non-zero -- checking for the known recoverable "
                              "post-process-step failure before treating this as a real failure")

    recovered = _find_recovered_output(requested_out)
    if recovered is None:
        raise RuntimeError(f"generative upscale segment {seg_index} produced no usable output "
                           f"(no recoverable .temp.*.mp4 file either)")

    out_w, out_h = _current_dims(recovered)
    if out_h < preset["output_tier_height"]:
        raise RuntimeError(f"generative upscale segment {seg_index} result is only {out_w}x{out_h}, "
                           f"below the {preset['output_tier_height']}p minimum tier")

    # The recovered .temp.*.mp4 file existing is NOT proof the diffusion pass
    # actually finished -- confirmed by testing: the same file shape (present,
    # valid header, missing audio) results from BOTH the known harmless
    # post-process bug (full-length, complete) AND a genuine mid-job CUDA OOM
    # crash (silently truncated to whatever had been produced so far). A
    # duration check is the only way to tell them apart.
    out_duration = _current_duration(recovered)
    tolerance = max(2.0, seg_duration * 0.1)
    if out_duration < seg_duration - tolerance:
        raise RuntimeError(
            f"generative upscale segment {seg_index} result is only {out_duration:.1f}s of an expected "
            f"{seg_duration:.1f}s -- likely a mid-job crash (e.g. CUDA OOM), not the known harmless "
            f"post-process-step failure"
        )
    db.log(job_id, STAGE, f"segment {seg_index} recovered: {recovered} ({out_w}x{out_h}, {out_duration:.1f}s)")

    if not is_whole_file:
        trimmed.unlink(missing_ok=True)
    if requested_out.exists() and requested_out != recovered:
        requested_out.unlink(missing_ok=True)

    return recovered, out_w, out_h


def _run_generative(job_id: str, job, settings: dict, preset: dict, src: Path) -> None:
    """Generative (Starlight) upscale, driven via neuroserver.exe directly --
    these models aren't reachable through ffmpeg's tvai_up filter at all
    (confirmed: 'astrasharp' etc. are absent from tvai_up's compiled model
    enum on this machine). See topaz_film_generative.json for the model
    choice and the known post-process bug this works around.

    Always processes the source as a sequence of segments cut at black-frame
    boundaries (found once via _blackdetect_boundaries, cached in
    settings_json) rather than one giant call -- these renders take days, and
    a crash, VRAM-fragmentation stop (see vram_probe), or a deliberate user
    pause can then only cost at most one segment's worth of work. Completed
    segments are copied to a persistent checkpoint dir (config.json's
    checkpoint_root -- NOT the R:\\ staging RAM disk, which is wiped on
    reboot) and recorded in settings_json so a retry/resume picks up where it
    left off instead of starting over."""
    if not NEUROSERVER:
        raise RuntimeError("config.json is missing tools.neuroserver -- required for the generative engine")

    total_duration = _current_duration(src)

    blackframes = settings.get("checkpoint_blackframes")
    if blackframes is None:
        blackframes = _blackdetect_boundaries(job_id, src, CONFIG["checkpoint_min_spacing_seconds"])
        settings = db.merge_settings(job_id, {"checkpoint_blackframes": blackframes})

    checkpoint_dir = Path(settings.get("checkpoint_dir") or str(Path(CONFIG["checkpoint_root"]) / job_id))
    if not settings.get("checkpoint_dir"):
        settings = db.merge_settings(job_id, {"checkpoint_dir": str(checkpoint_dir)})

    completed: list[dict] = settings.get("checkpoint_completed_segments") or []
    resume_from = completed[-1]["end"] if completed else 0.0
    expected_w: Optional[int] = None
    expected_h: Optional[int] = None
    if completed:
        expected_w, expected_h = _current_dims(Path(completed[0]["path"]))

    # A blackdetect boundary can legitimately land a fraction of a second
    # before the true end of the file (confirmed on a real job: 433.08s vs.
    # an actual 433.566s duration) -- close enough that it's not a real
    # internal split, just noise in exactly where "black" starts. Treating
    # it as one anyway would spin up a whole extra neuroserver invocation
    # (~80s of model warm-up alone) for a leftover clip a fraction of a
    # second long. Reuse checkpoint_min_spacing_seconds as "close enough to
    # the end to just extend the last segment to it" -- it's already the
    # project's own definition of "not a meaningfully separate checkpoint."
    min_spacing = CONFIG["checkpoint_min_spacing_seconds"]
    while resume_from < total_duration - 0.05:
        next_boundary = next((t for t in blackframes if t > resume_from + 1e-3), None)
        is_final = next_boundary is None or next_boundary >= total_duration - min_spacing
        seg_end = None if is_final else next_boundary
        seg_end_display = total_duration if seg_end is None else seg_end
        seg_duration = seg_end_display - resume_from

        db.log(job_id, STAGE, f"generative segment {len(completed)}: [{resume_from:.2f}s, {seg_end_display:.2f}s) "
                              f"of {total_duration:.2f}s total ({len(completed)} segment(s) already checkpointed)")

        if not vram_probe.can_allocate(preset["max_gpu_mem_gb"]):
            db.log(job_id, STAGE, f"VRAM pre-check failed -- couldn't allocate a contiguous "
                                  f"{preset['max_gpu_mem_gb']}GB block (likely fragmentation, needs a "
                                  f"restart to clear) -- parking job as needs_restart")
            _persist_input_if_volatile(job_id, src, checkpoint_dir)
            db.update_job(job_id, status="needs_restart",
                          error_message="VRAM allocation pre-check failed before generative upscale -- "
                                        "likely fragmentation; will auto-retry after a server/machine restart")
            return

        recovered, out_w, out_h = _run_generative_segment(
            job_id, preset, src, resume_from, seg_end, seg_duration, total_duration, len(completed),
        )
        if expected_w is None:
            expected_w, expected_h = out_w, out_h
        elif (out_w, out_h) != (expected_w, expected_h):
            raise RuntimeError(f"generative upscale segment {len(completed)} came out {out_w}x{out_h}, "
                               f"expected {expected_w}x{expected_h} to match earlier checkpointed segments")

        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        saved_path = checkpoint_dir / f"segment_{len(completed):04d}.mp4"
        # shutil.move (not Path.replace/os.replace) -- checkpoint_dir is
        # deliberately on a different, persistent drive than the R:\ staging
        # area `recovered` lives on, and os.replace() can't rename across
        # volumes on Windows.
        shutil.move(str(recovered), str(saved_path))
        completed = completed + [{"start": resume_from, "end": seg_end_display, "path": str(saved_path)}]
        settings = db.merge_settings(job_id, {"checkpoint_completed_segments": completed})
        resume_from = seg_end_display

        # Only pause here if there's actually more source left -- if that was
        # the last segment, fall through to the final concat/remux below
        # instead of stranding a fully-complete job in "paused" forever.
        if resume_from < total_duration - 0.05 and procutil.should_pause_now():
            db.log(job_id, STAGE, f"pausing at checkpoint boundary by user request "
                                  f"({len(completed)} segment(s) complete, resuming from {resume_from:.2f}s)")
            db.update_job(job_id, status="paused")
            return

    # All segments complete -- concatenate them (each a video-only recovered
    # output) and remux the ORIGINAL full source's audio once at the end,
    # rather than per segment -- simpler, and avoids needing to slice the
    # audio track to match each segment's exact boundaries.
    out_w, out_h = expected_w, expected_h
    if len(completed) == 1:
        concat_src = Path(completed[0]["path"])
    else:
        concat_list = src.with_name(src.stem + "_upscaled_concat_list.txt")
        concat_list.write_text(
            "\n".join(f"file '{Path(seg['path']).as_posix()}'" for seg in completed) + "\n",
            encoding="utf-8",
        )
        concat_src = src.with_name(src.stem + "_upscaled_concat.mp4")
        cmd = [FFMPEG, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
               "-i", str(concat_list), "-c", "copy", str(concat_src)]
        db.log(job_id, STAGE, f"concatenating {len(completed)} checkpoint segments -> {concat_src}")
        run_logged(job_id, STAGE, cmd)
        if not concat_src.exists() or concat_src.stat().st_size == 0:
            raise RuntimeError("checkpoint segment concat produced no output file")
        concat_list.unlink(missing_ok=True)

    out_path = src.with_name(src.stem + "_upscaled." + preset["encoder"]["container"])
    # The concatenated segments have no audio (each recovered segment file is
    # audio-less -- see _run_generative_segment) -- remux the source's full
    # audio track back in via a plain stream copy (no re-encode).
    cmd = [
        FFMPEG, "-hide_banner", "-y",
        "-i", str(concat_src),
        "-i", str(src),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c", "copy",
        str(out_path),
    ]
    db.log(job_id, STAGE, "muxing original audio back into the concatenated generative output")
    run_logged(job_id, STAGE, cmd)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("audio remux for generative upscale produced no output file")

    if concat_src != out_path:
        concat_src.unlink(missing_ok=True)

    # Job succeeded end to end -- the checkpoint segments/settings have done
    # their job, clean them up.
    for seg in completed:
        Path(seg["path"]).unlink(missing_ok=True)
    try:
        checkpoint_dir.rmdir()
    except OSError:
        pass  # not empty (unexpected leftover) or already gone -- best-effort
    settings = db.merge_settings(job_id, {
        "checkpoint_completed_segments": None,
        "checkpoint_blackframes": None,
        "checkpoint_dir": None,
    })

    upscale_summary = (f"Topaz {preset['model_display_name']} ({preset['model']}), generative, "
                       f"{len(completed)} checkpoint segment(s), result={out_w}x{out_h}")
    display_name = settings.get("display_filename") or job["original_filename"]
    new_name = naming.set_upscaled_tag(display_name, out_h)

    db.log(job_id, STAGE, f"generative upscale complete -> {out_path}")
    db.update_job(job_id, stage=STAGE, status="pending", current_file=str(out_path))
    db.merge_settings(job_id, {
        "display_filename": new_name,
        "upscaled_width": out_w, "upscaled_height": out_h,
        "upscale_summary": upscale_summary,
    })
