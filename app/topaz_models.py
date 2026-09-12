"""Shared helpers for reasoning about Topaz Video AI model capabilities --
used by the upscale stage (to actually build the ffmpeg filter chain) and by
the API (to describe the strategy in the UI's preset detail panel), so both
stay in sync automatically instead of duplicating the logic."""
from __future__ import annotations

import json
from pathlib import Path

from .config import CONFIG


def available_scales(model: str) -> list[int]:
    """Each Topaz model only supports specific integer scales (e.g. gcg-5 is
    1/2/4, ganim-1 is only 2 -- confirmed by testing: ffmpeg rejects "Invalid
    scale 3 for model gcg-5, allowed scales are: 1, 2, 4"). Read the model's
    own JSON rather than hardcoding a set that only fits one model."""
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


def build_scale_passes(source_h: int, target_h: int, model: str) -> list[int]:
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
    options = available_scales(model)
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


def describe_upscale_strategy(model: str) -> str:
    """Plain-language explanation of how this model reaches an arbitrary
    target tier, for the UI's human-readable preset panel."""
    scales = available_scales(model)
    if len(scales) == 1:
        s = scales[0]
        return (
            f"This model only supports a fixed {s}x AI upscale per pass. "
            f"When the target tier needs more than {s}x, the preset chains "
            f"repeated {s}x passes (e.g. {s}x → {s}x = {s * s}x) rather than "
            f"falling back to a plain resize for the shortfall, then a final "
            f"precise resize lands exactly on the target resolution."
        )
    scale_list = ", ".join(f"{s}x" for s in scales)
    return (
        f"This model supports {scale_list} passes. The smallest scale that "
        f"alone covers the needed ratio is used in a single pass, then a "
        f"final precise resize lands exactly on the target resolution."
    )
