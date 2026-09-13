"""Filename tag handling.

Source files carry bracket tags like `[DVD]`, `[Bluray]`, `[480i]`. We preserve
source-type tags untouched and evolve a single "status" tag as the file moves
through the pipeline:

    Show S01E01 [DVD] [480i].mkv
        -> deinterlace ->  Show S01E01 [DVD] [480p].mkv
        -> denoise (opt) -> Show S01E01 [DVD] [480p Denoised].mkv
        -> upscale ->       Show S01E01 [Upscaled 1080p].mkv

Through deinterlace/denoise, only the status tag (matching RES_TAG_RE) is
touched -- other bracket tags (e.g. [DVD]) pass through unchanged. At the
final upscale step the whole tag list is replaced by the single "Upscaled
Np" tag -- the source-type/interim tags are no longer relevant once the
deliverable exists.
"""
from __future__ import annotations

import re
from pathlib import Path

TAG_RE = re.compile(r"\[([^\[\]]+)\]")
RES_TAG_RE = re.compile(r"^\d+[ip](\s+\w+)*$|^Upscaled\s+\d+p$", re.IGNORECASE)


def split_tags(filename: str) -> tuple[str, list[str], str]:
    """Returns (base_title, tags, extension) e.g.
    'Show S01E01 [DVD] [480i].mkv' -> ('Show S01E01', ['DVD', '480i'], '.mkv')
    """
    path = Path(filename)
    stem, ext = path.stem, path.suffix
    tags = TAG_RE.findall(stem)
    base = TAG_RE.sub("", stem).strip()
    base = re.sub(r"\s{2,}", " ", base)
    return base, tags, ext


def join_tags(base: str, tags: list[str], ext: str) -> str:
    parts = [base] + [f"[{t}]" for t in tags if t]
    return " ".join(parts).strip() + ext


def _replace_status_tag(tags: list[str], new_tag: str) -> list[str]:
    out = []
    replaced = False
    for t in tags:
        if RES_TAG_RE.match(t.strip()):
            if not replaced:
                out.append(new_tag)
                replaced = True
            # drop any further matches (shouldn't normally happen)
        else:
            out.append(t)
    if not replaced:
        out.append(new_tag)
    return out


def set_progressive_tag(filename: str, height: int) -> str:
    base, tags, ext = split_tags(filename)
    tags = _replace_status_tag(tags, f"{height}p")
    return join_tags(base, tags, ext)


def add_denoised_tag(filename: str) -> str:
    base, tags, ext = split_tags(filename)
    out = []
    for t in tags:
        if RES_TAG_RE.match(t.strip()) and "denoised" not in t.lower() and not t.lower().startswith("upscaled"):
            out.append(f"{t} Denoised")
        else:
            out.append(t)
    return join_tags(base, out, ext)


def add_dehalo_tag(filename: str) -> str:
    base, tags, ext = split_tags(filename)
    out = []
    for t in tags:
        if RES_TAG_RE.match(t.strip()) and "dehalo" not in t.lower() and not t.lower().startswith("upscaled"):
            out.append(f"{t} Dehalo")
        else:
            out.append(t)
    return join_tags(base, out, ext)


def set_upscaled_tag(filename: str, height: int) -> str:
    """The final delivered filename keeps only the Upscaled tag -- source-type
    tags like [DVD]/[Bluray] and interim ones like [Denoised] are dropped."""
    base, _tags, ext = split_tags(filename)
    return join_tags(base, [f"Upscaled {height}p"], ext)


def set_final_progressive_tag(filename: str, height: int, extra: list[str] | None = None) -> str:
    """Terminal filename for a job that never reached the Upscale stage: a
    plain resolution tag (e.g. '[480p]') plus any of the stage suffixes that
    actually applied (e.g. '[480p Dehalo]', '[480p Denoised Dehalo]') -- via
    `extra`, since with Denoise/Dehalo/Upscale as independent toggles there's
    no later stage to make an interim tag redundant the way `set_upscaled_tag`
    assumes. Source-type tags like [DVD]/[Bluray] are still dropped."""
    base, _tags, ext = split_tags(filename)
    tag = " ".join([f"{height}p", *(extra or [])])
    return join_tags(base, [tag], ext)


def has_progressive_res_tag(filename: str) -> bool:
    """True if the filename already carries a plain progressive-resolution
    tag (e.g. '[1080p]', not '[480i]') -- used to no-op skip-upscale jobs
    whose source is already a finished progressive deliverable."""
    _, tags, _ = split_tags(filename)
    return any(re.match(r"^\d+p$", t.strip(), re.IGNORECASE) for t in tags)


def detect_source_scan_hint(filename: str) -> str | None:
    """Best-effort seed from an existing [480i]/[480p]-style tag, if present."""
    _, tags, _ = split_tags(filename)
    for t in tags:
        m = re.match(r"^(\d+)([ip])$", t.strip(), re.IGNORECASE)
        if m:
            return t.strip().lower()
    return None


_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_working_name(job_id: str, ext: str) -> str:
    """Short ASCII-only filename for tools (some CLI tools choke on long/unicode
    names). The mapping back to the real name lives in the job's DB row."""
    ext = ext if ext.startswith(".") else f".{ext}"
    return f"job_{job_id}{ext}"
