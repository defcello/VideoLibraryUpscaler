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


# NVDEC doesn't carry "unspecified" color metadata through -- on untagged
# sources (common for MakeMKV DVD rips) the frames come out tagged
# csp:gbr prim:reserved trc:reserved, which swscale refuses to convert to
# tvai_up's rgb48le ("Unsupported input", error -129). Re-tag them before
# any tvai filter, keeping real source tags and defaulting the rest by
# resolution (BT.601/SMPTE-170M for SD, BT.709 otherwise).
_UNSET = {"", "unknown", "unspecified", "reserved", "gbr"}


def color_tag_filter(path: Path, height: int) -> str:
    try:
        proc = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=color_space,color_primaries,color_transfer,color_range",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        tags = json.loads(proc.stdout)["streams"][0]
    except Exception:
        tags = {}
    sd = height <= 576
    defaults = {
        "color_space": "smpte170m" if sd else "bt709",
        "color_primaries": ("bt470bg" if height == 576 else "smpte170m") if sd else "bt709",
        "color_transfer": "smpte170m" if sd else "bt709",
        "color_range": "tv",
    }
    v = {k: (tags.get(k) if (tags.get(k) or "") not in _UNSET else d) for k, d in defaults.items()}
    return (f"setparams=colorspace={v['color_space']}:color_primaries={v['color_primaries']}"
            f":color_trc={v['color_transfer']}:range={v['color_range']}")
