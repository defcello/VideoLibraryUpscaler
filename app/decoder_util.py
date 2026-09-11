"""Topaz's bundled ffmpeg was built with `--disable-decoder=h264
--disable-decoder=hevc` and no software fallback -- only hardware decoders
(h264_qsv/h264_amf/h264_cuvid etc). On an NVIDIA-only machine its own
"auto-pick a hwaccel" logic defaults to QSV and fails hard with no Intel GPU
present. We work around this by explicitly forcing the matching NVIDIA CUVID
decoder for every ffmpeg call that touches Topaz's ffmpeg and actually
decodes the source video (probe's idet/cropdetect, the upscale stage)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import CONFIG

FFPROBE = CONFIG["tools"]["ffprobe"]

CODEC_TO_CUVID = {
    "h264": "h264_cuvid",
    "hevc": "hevc_cuvid",
    "mpeg1video": "mpeg1_cuvid",
    "mpeg2video": "mpeg2_cuvid",
    "mpeg4": "mpeg4_cuvid",
    "vc1": "vc1_cuvid",
    "vp8": "vp8_cuvid",
    "vp9": "vp9_cuvid",
    "av1": "av1_cuvid",
}


def cuvid_decoder_args(path: Path) -> list[str]:
    """Returns e.g. ["-c:v", "h264_cuvid"] for this file's video codec, or []
    if the codec isn't in the map (caller falls back to ffmpeg's default,
    which may still fail on QSV-only paths -- logged so it's visible)."""
    try:
        proc = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        codec = json.loads(proc.stdout)["streams"][0]["codec_name"]
    except Exception:
        return []
    decoder = CODEC_TO_CUVID.get(codec)
    return ["-c:v", decoder] if decoder else []
