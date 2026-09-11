"""Stage 1: figure out what kind of source we're dealing with and how
confident we are about it.

Three-tier detection, cheapest first:
  1. ffprobe container metadata (fast sanity check / seed).
  2. `ffmpeg -vf idet` sampled at a few points -> progressive vs interlaced
     frame ratios, and which field is dominant if interlaced.
  3. Only when idet is ambiguous (i.e. it's not overwhelmingly progressive
     nor overwhelmingly one dominant field): a real VIVTC VFM dry-run over a
     sample of frames, checking how much combing survives field-matching.
     This is the cadence signal that distinguishes telecined 24p from true
     interlaced 60i -- the case the guide explicitly warns idet-style ratios
     alone can get wrong.

A job only auto-proceeds past this stage when its confidence clears
config.json's probe.confidence_threshold; otherwise it's marked
`needs_review` so a bad guess doesn't get committed to on an unattended
multi-hour render.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from .. import db, naming
from ..config import CONFIG
from ..decoder_util import cuvid_decoder_args
from ..vpy_render import render

STAGE = "probed"

FFPROBE = CONFIG["tools"]["ffprobe"]
FFMPEG = CONFIG["tools"]["ffmpeg"]
VS_PYTHON = CONFIG["tools"]["vapoursynth_python"]

MULTI_FRAME_RE = re.compile(
    r"Multi frame detection:\s*TFF:\s*(\d+)\s*BFF:\s*(\d+)\s*Progressive:\s*(\d+)\s*Undetermined:\s*(\d+)"
)
CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def _ffprobe_json(path: Path) -> dict:
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def _video_stream(info: dict) -> dict:
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    raise RuntimeError("no video stream found")


def _parse_par(stream: dict) -> tuple[int, int]:
    sar = stream.get("sample_aspect_ratio", "1:1")
    if not sar or sar == "0:1":
        return 1, 1
    num, _, den = sar.partition(":")
    try:
        return int(num), int(den)
    except ValueError:
        return 1, 1


def _run_idet_sample(job_id: str, path: Path, start_seconds: float, duration_seconds: float) -> Optional[tuple[int, int, int, int]]:
    decoder_args = cuvid_decoder_args(path)
    proc = subprocess.run(
        [FFMPEG, *decoder_args, "-ss", str(start_seconds), "-i", str(path), "-t", str(duration_seconds),
         "-filter:v", "idet", "-an", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    matches = MULTI_FRAME_RE.findall(proc.stderr)
    db.log(job_id, STAGE, f"idet sample @ {start_seconds:.0f}s: " + (str(matches[-1]) if matches else "no match found"))
    if not matches:
        return None
    tff, bff, prog, undet = (int(x) for x in matches[-1])
    return tff, bff, prog, undet


def _run_cropdetect_sample(path: Path, start_seconds: float, duration_seconds: float) -> Optional[tuple[int, int, int, int]]:
    decoder_args = cuvid_decoder_args(path)
    proc = subprocess.run(
        [FFMPEG, *decoder_args, "-ss", str(start_seconds), "-i", str(path), "-t", str(duration_seconds),
         "-filter:v", "cropdetect=round=2", "-an", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    matches = CROP_RE.findall(proc.stderr)
    if not matches:
        return None
    w, h, x, y = (int(v) for v in matches[-1])
    return w, h, x, y


def _cadence_dry_run(job_id: str, path: Path, tff: bool, sample_frames: int = 300) -> Optional[float]:
    script = render("probe_cadence.py.j2", source_path=str(path), tff=tff, sample_frames=sample_frames)
    script_path = path.parent / f"{path.stem}_cadence_probe.py"
    script_path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [VS_PYTHON, str(script_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    db.log(job_id, STAGE, f"cadence probe (tff={tff}) stdout tail: {proc.stdout[-500:]}")
    if proc.returncode != 0:
        db.log(job_id, STAGE, f"cadence probe failed: {proc.stderr[-1500:]}")
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("PROBE_RESULT:"):
            data = json.loads(line[len("PROBE_RESULT:"):])
            return data.get("combed_after_vfm_frac")
    return None


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    path = Path(job["current_file"])
    threshold = CONFIG["probe"]["confidence_threshold"]

    info = _ffprobe_json(path)
    vstream = _video_stream(info)
    width = int(vstream["width"])
    height = int(vstream["height"])
    par_num, par_den = _parse_par(vstream)
    duration = float(info.get("format", {}).get("duration") or vstream.get("duration") or 0)
    container_field_order = vstream.get("field_order", "unknown")  # progressive/tt/bb/tb/bt/unknown
    db.log(job_id, STAGE, f"ffprobe: {width}x{height} PAR {par_num}:{par_den} field_order={container_field_order} duration={duration:.1f}s")

    # --- idet sampling across a few points in the file ---
    n_points = CONFIG["probe"]["idet_sample_points"]
    sample_seconds = CONFIG["probe"]["idet_sample_seconds"]
    totals = [0, 0, 0, 0]  # tff, bff, progressive, undetermined
    got_any = False
    for i in range(n_points):
        offset = duration * (i + 1) / (n_points + 1) if duration else 0
        result = _run_idet_sample(job_id, path, offset, sample_seconds)
        if result:
            got_any = True
            for j in range(4):
                totals[j] += result[j]

    crop = _run_cropdetect_sample(path, duration / 2 if duration else 0, min(sample_seconds, duration or sample_seconds))
    crop_left = crop_top = crop_right = crop_bottom = 0
    if crop:
        cw, ch, cx, cy = crop
        crop_left = cx
        crop_top = cy
        crop_right = max(0, width - cw - cx)
        crop_bottom = max(0, height - ch - cy)
        db.log(job_id, STAGE, f"cropdetect: crop={cw}:{ch}:{cx}:{cy} -> L{crop_left} R{crop_right} T{crop_top} B{crop_bottom}")

    settings = {
        "width": width, "height": height,
        "par_num": par_num, "par_den": par_den,
        "duration": duration,
        "container_field_order": container_field_order,
        "crop_left": crop_left, "crop_right": crop_right,
        "crop_top": crop_top, "crop_bottom": crop_bottom,
    }

    if not got_any:
        db.merge_settings(job_id, settings)
        db.update_job(job_id, stage=STAGE, status="needs_review",
                       error_message="idet produced no usable output; inspect source manually")
        db.log(job_id, STAGE, "no idet results at all -- flagging for review")
        return

    tff_n, bff_n, prog_n, undet_n = totals
    total_n = sum(totals) or 1
    prog_frac = prog_n / total_n
    tff_frac = tff_n / total_n
    bff_frac = bff_n / total_n
    db.log(job_id, STAGE, f"idet totals: TFF={tff_n} BFF={bff_n} Progressive={prog_n} Undetermined={undet_n} (prog_frac={prog_frac:.2f})")

    scan_type: str
    tff: Optional[bool] = None
    confidence: float
    reason = ""

    if prog_frac >= 0.90:
        scan_type = "progressive"
        confidence = prog_frac
    elif max(tff_frac, bff_frac) >= 0.60 and prog_frac < 0.15:
        scan_type = "interlaced"
        tff = tff_frac >= bff_frac
        confidence = max(tff_frac, bff_frac) / (1 - prog_frac) if prog_frac < 1 else max(tff_frac, bff_frac)
        confidence = min(confidence, 0.99)
    else:
        # Ambiguous by idet alone -- this is the classic telecine signature
        # (idet sees combing throughout because it's still 60 fields/sec) or
        # a genuinely mixed-cadence source. Run the real VFM dry-run.
        assumed_tff = container_field_order not in ("bb", "bt") if container_field_order != "unknown" else True
        combed_frac = _cadence_dry_run(job_id, path, assumed_tff)
        if combed_frac is None:
            scan_type = "interlaced"
            tff = tff_frac >= bff_frac
            confidence = 0.0
            reason = "idet ambiguous and cadence dry-run failed"
        elif combed_frac <= 0.08:
            scan_type = "telecine"
            tff = assumed_tff
            confidence = 1 - combed_frac  # near-zero leftover combing => high confidence in this cadence/order
        else:
            # try the opposite field order once before giving up
            combed_frac_opp = _cadence_dry_run(job_id, path, not assumed_tff)
            if combed_frac_opp is not None and combed_frac_opp < combed_frac and combed_frac_opp <= 0.08:
                scan_type = "telecine"
                tff = not assumed_tff
                confidence = 1 - combed_frac_opp
            else:
                scan_type = "interlaced" if prog_frac < 0.5 else "progressive"
                tff = tff_frac >= bff_frac
                confidence = 0.0
                reason = (f"neither field order field-matched cleanly (combed_frac={combed_frac:.2f} / "
                          f"{combed_frac_opp if combed_frac_opp is not None else 'n/a'}); likely mixed "
                          f"cadence or a fake-interlaced source per the guide's warning")

    settings.update({
        "scan_type": scan_type,
        "tff": tff,
        "confidence": confidence,
        "idet_tff_frac": tff_frac, "idet_bff_frac": bff_frac, "idet_progressive_frac": prog_frac,
    })
    db.merge_settings(job_id, settings)

    if confidence >= threshold:
        db.log(job_id, STAGE, f"scan_type={scan_type} tff={tff} confidence={confidence:.2f} -- auto-proceeding")
        db.update_job(job_id, stage=STAGE, status="pending")
    else:
        msg = reason or f"confidence {confidence:.2f} below threshold {threshold:.2f}"
        db.log(job_id, STAGE, f"scan_type={scan_type} tff={tff} confidence={confidence:.2f} -- NEEDS REVIEW: {msg}")
        db.update_job(job_id, stage=STAGE, status="needs_review", error_message=msg)
