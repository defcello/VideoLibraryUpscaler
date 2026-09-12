"""Embeds a summary of what this pipeline did into the output file's own
container metadata (matroska/mp4 tags), so opening the file later (mkvinfo,
MediaInfo, VLC's codec info, etc.) shows the processing history relative to
the original -- without needing this tool's database."""
from __future__ import annotations

from datetime import datetime, timezone


def ffmpeg_metadata_args(tags: dict) -> list[str]:
    args = []
    for key, value in tags.items():
        if value:
            args.extend(["-metadata", f"{key}={value}"])
    return args


def processed_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
