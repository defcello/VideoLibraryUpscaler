"""Library view support: browser roots, per-directory settings files with
inheritance, workflow definitions, output path/filename templating, and
"has this file already been processed by this workflow?" detection.

Settings files
--------------
Any directory may hold a `.ai-remaster-pipeline.settings.json`:

    {
        "ignore_parent_settings": false,
        "default_workflow": "Upscale 1080p",
        "workflows": {
            "Custom":        { ...fields... },   # overrides the directory's defaults
            "Upscale 1080p": { ...fields... }
        }
    }

For a target directory, parent directories are walked up to the drive/share
root, stopping early at the first settings file with `ignore_parent_settings`
set. The chain is then applied root -> target over the app defaults. A
workflow's fields are shallow-merged over the same-named workflow inherited
from the parent; a workflow name not seen before starts from the "Custom"
workflow as resolved so far (so overriding "Custom" in a directory changes
the defaults every other workflow defined at or below it builds on).

Templates
---------
Path fields (temp/milestone/output dirs) and the target filename share one
syntax: `${name}` variables, `$1`..`$9` / `${1}` regex group references from
the base filename regex, and the escapes `$$` (literal `$`), `${lb}` (`{`)
and `${rb}` (`}`). Bare `{`/`}` outside a `${...}` are literal too.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from typing import Optional

from . import db
from .config import CONFIG, PROJECT_ROOT, load_preset, resolve_workflow
from .naming import detect_source_scan_hint, split_tags

SETTINGS_NAME = ".ai-remaster-pipeline.settings.json"
LIBRARY_STATE_PATH = PROJECT_ROOT / "library.json"
CUSTOM = "Custom"

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".mpg", ".mpeg", ".m2v", ".ts", ".m2ts", ".mts",
    ".vob", ".wmv", ".webm", ".flv", ".ogv", ".divx", ".3gp",
}
# finalize.py always delivers Matroska (MP4 silently drops custom metadata).
OUTPUT_EXT = ".mkv"

PROCESSING_STAGES = ("deinterlace", "denoise", "deblur", "dehalo", "upscale")


def app_default_workflow() -> dict:
    return {
        "show_regex": ".*",
        "hide_regex": "(?!)",
        "include_subdirs": False,
        "show_all": False,
        "show_hidden": False,
        "temp_dir": str(Path(CONFIG["staging_drives"][0]) / CONFIG["staging_subdir"]),
        "milestone_dir": CONFIG["checkpoint_root"],
        "output_dir": "${dir-source}",
        "base_regex": r"^(.*)\.[^\.]+$",
        "target_format": "$1 [${workflow-tag} ${output-vresmode}]${ext}",
        # "[Upscaled 1080p]" is what the pre-workflow pipeline produced, so the
        # default workflow's filename fallback recognizes those outputs too.
        "workflow_tag": "Upscaled",
        "crop_enabled": False,
        "crop_start": "",
        "crop_end": "",
        "deinterlace_enabled": True,
        "denoise_enabled": False,
        "deblur_enabled": False,
        "dehalo_enabled": False,
        "upscale_enabled": True,
        "allow_generative": False,
        "content_type": load_preset("content_types")["default"],
    }


WORKFLOW_FIELDS = tuple(app_default_workflow().keys())


def workflow_stages(wf: dict) -> tuple[str, ...]:
    return tuple(s for s in PROCESSING_STAGES if wf.get(f"{s}_enabled"))


def job_stages(job) -> tuple[str, ...]:
    flags = {
        "deinterlace": job["deinterlace_enabled"], "denoise": job["denoise_enabled"],
        "deblur": job["deblur_enabled"], "dehalo": job["dehalo_enabled"], "upscale": not job["skip_upscale"],
    }
    return tuple(s for s in PROCESSING_STAGES if flags[s])


# ------------------------------------------------------------------- roots

_state_lock = threading.Lock()


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def get_roots() -> list[str]:
    try:
        with open(LIBRARY_STATE_PATH, "r", encoding="utf-8") as f:
            roots = json.load(f).get("roots")
        if isinstance(roots, list) and roots:
            return [str(r) for r in roots]
    except (OSError, ValueError):
        pass
    return [str(Path.home())]


def set_roots(roots: list[str]) -> list[str]:
    cleaned, seen = [], set()
    for r in roots:
        p = str(Path(r))
        if _norm(p) not in seen:
            seen.add(_norm(p))
            cleaned.append(p)
    with _state_lock:
        state = {}
        try:
            with open(LIBRARY_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            pass
        state["roots"] = cleaned
        with open(LIBRARY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
    return cleaned


def root_for(path: str | Path) -> Optional[str]:
    """The deepest configured browser root containing `path` (None if none)."""
    target = _norm(path)
    best = None
    for r in get_roots():
        nr = _norm(r)
        if target == nr or target.startswith(nr.rstrip("\\/") + os.sep):
            if best is None or len(nr) > len(_norm(best)):
                best = r
    return best


# ----------------------------------------------------------- settings files

class SettingsError(ValueError):
    pass


def read_local(directory: str | Path) -> Optional[dict]:
    """The directory's own settings file, or None if it has none. A file that
    can't be parsed raises SettingsError rather than being silently ignored --
    quietly falling back to defaults would route outputs somewhere unexpected."""
    path = Path(directory) / SETTINGS_NAME
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise SettingsError(f"{path}: {e}") from e
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: top level must be a JSON object")
    return data


def validate_local(data: dict) -> dict:
    if not isinstance(data, dict):
        raise SettingsError("settings must be a JSON object")
    out: dict = {}
    if data.get("ignore_parent_settings"):
        out["ignore_parent_settings"] = True
    if data.get("default_workflow"):
        out["default_workflow"] = str(data["default_workflow"])
    workflows = data.get("workflows") or {}
    if not isinstance(workflows, dict):
        raise SettingsError("'workflows' must be an object of name -> fields")
    clean_wfs = {}
    for name, fields in workflows.items():
        name = str(name).strip()
        if not name:
            raise SettingsError("workflow names can't be blank")
        if not isinstance(fields, dict):
            raise SettingsError(f"workflow {name!r} must be an object")
        unknown = set(fields) - set(WORKFLOW_FIELDS)
        if unknown:
            raise SettingsError(f"workflow {name!r} has unknown field(s): {', '.join(sorted(unknown))}")
        for key in ("show_regex", "hide_regex", "base_regex"):
            if key in fields:
                try:
                    re.compile(fields[key])
                except re.error as e:
                    raise SettingsError(f"workflow {name!r} {key}: {e}") from e
        clean_wfs[name] = dict(fields)
    if clean_wfs:
        out["workflows"] = clean_wfs
    return out


def write_local(directory: str | Path, data: dict) -> dict:
    data = validate_local(data)
    path = Path(directory) / SETTINGS_NAME
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        f.write("\n")
    return data


def settings_chain(directory: str | Path, local_override: Optional[dict] = None) -> list[tuple[str, Optional[dict]]]:
    """[(dir, settings-or-None), ...] from the inheritance root down to
    `directory`. `local_override` stands in for `directory`'s own file (used
    to resolve unsaved edits)."""
    target = Path(directory)
    chain: list[tuple[str, Optional[dict]]] = []
    current = target
    while True:
        data = local_override if (local_override is not None and current == target) else read_local(current)
        chain.append((str(current), data))
        if data and data.get("ignore_parent_settings"):
            break
        if current.parent == current:
            break
        current = current.parent
    chain.reverse()
    return chain


def resolve(directory: str | Path, local_override: Optional[dict] = None) -> dict:
    defaults = app_default_workflow()
    workflows: dict[str, dict] = {CUSTOM: {**defaults, "defined_in": None}}
    default_workflow = None
    chain = settings_chain(directory, local_override)
    for d, data in chain:
        if not data:
            continue
        if data.get("ignore_parent_settings"):
            workflows = {CUSTOM: {**defaults, "defined_in": None}}
            default_workflow = None
        if data.get("default_workflow"):
            default_workflow = data["default_workflow"]
        local_wfs = data.get("workflows") or {}
        # Custom first so other workflows defined in this same file build on it.
        for name in sorted(local_wfs, key=lambda n: n != CUSTOM):
            fields = {k: v for k, v in local_wfs[name].items() if k in WORKFLOW_FIELDS}
            base = workflows.get(name) or workflows[CUSTOM]
            workflows[name] = {**base, **fields, "defined_in": d}
    if default_workflow not in workflows:
        default_workflow = CUSTOM
    return {
        "chain": [d for d, _ in chain],
        "settings_files": [d for d, data in chain if data is not None],
        "workflows": workflows,
        "default_workflow": default_workflow,
    }


def save_workflow(directory: str, name: str, fields: dict) -> dict:
    """Writes `name` into `directory`'s settings file as an override holding
    only the fields that differ from what it would inherit without it."""
    name = name.strip()
    if not name:
        raise SettingsError("workflow name can't be blank")
    local = read_local(directory) or {}
    wfs = dict(local.get("workflows") or {})
    wfs.pop(name, None)
    without = resolve(directory, {**local, "workflows": wfs})
    base = without["workflows"].get(name) or without["workflows"][CUSTOM]
    diff = {k: fields[k] for k in WORKFLOW_FIELDS if k in fields and base.get(k) != fields[k]}
    wfs[name] = diff
    return write_local(directory, {**local, "workflows": wfs})


def rename_workflow(directory: str, old: str, new: str) -> dict:
    new = new.strip()
    local = read_local(directory) or {}
    wfs = dict(local.get("workflows") or {})
    if old not in wfs:
        raise SettingsError(f"{old!r} isn't defined in this directory's settings file (it's inherited) -- "
                            "rename it where it's defined")
    if old == CUSTOM:
        raise SettingsError("'Custom' can't be renamed")
    if not new or new in wfs:
        raise SettingsError(f"invalid or duplicate workflow name: {new!r}")
    wfs = {(new if k == old else k): v for k, v in wfs.items()}
    if local.get("default_workflow") == old:
        local["default_workflow"] = new
    return write_local(directory, {**local, "workflows": wfs})


def delete_workflow(directory: str, name: str) -> dict:
    local = read_local(directory) or {}
    wfs = dict(local.get("workflows") or {})
    if name not in wfs:
        raise SettingsError(f"{name!r} isn't defined in this directory's settings file (it's inherited)")
    del wfs[name]
    if local.get("default_workflow") == name:
        local.pop("default_workflow")
    return write_local(directory, {**local, "workflows": wfs})


# ---------------------------------------------------------------- templates

_TOKEN_RE = re.compile(r"\$\$|\$\{([^{}]*)\}|\$(\d)")


class TemplateError(ValueError):
    pass


def expand(template: str, variables: dict, groups: tuple = (), keep: tuple = ()) -> str:
    """Expands a template (syntax in the module docstring). Variables named
    in `keep` are left as `${name}` tokens -- and literal `$` re-escaped as
    `$$` -- so the result can be expanded again later (finalize.py fills
    `${output-vresmode}` once the real output resolution is known)."""
    partial = bool(keep)

    def sub(m: re.Match) -> str:
        if m.group(0) == "$$":
            return "$$" if partial else "$"
        name = m.group(1) if m.group(1) is not None else m.group(2)
        if name == "lb":
            return "{"
        if name == "rb":
            return "}"
        if name.isdigit():
            i = int(name)
            value = groups[i - 1] if 0 < i <= len(groups) and groups[i - 1] is not None else ""
        elif name in keep:
            return m.group(0)
        elif name in variables:
            value = str(variables[name])
        else:
            raise TemplateError(f"unknown variable ${{{name}}}")
        return value.replace("$", "$$") if partial else value

    return _TOKEN_RE.sub(sub, template)


_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str) -> str:
    name = _INVALID_NAME_CHARS.sub("_", name).strip().rstrip(".")
    suffix = Path(name).suffix.lower()
    if suffix in VIDEO_EXTS:
        name = name[: -len(suffix)] + OUTPUT_EXT
    elif suffix != OUTPUT_EXT:
        name += OUTPUT_EXT
    return name


def dir_variables(source: Path, workflow_dir: str) -> dict:
    return {
        "dir-source": str(source.parent),
        "dir-workflow": str(workflow_dir),
        "dir-root": root_for(source) or str(Path(source.anchor or source.parent)),
    }


def resolve_dir(template: str, source: Path, workflow_dir: str) -> str:
    """Expands a directory template; relative results are relative to the
    source file's own directory."""
    text = expand(template or "${dir-source}", dir_variables(source, workflow_dir)).strip()
    p = Path(text)
    if not p.is_absolute() and not PureWindowsPath(text).drive:
        p = source.parent / p
    return os.path.normpath(str(p))


def name_template(wf: dict, source: Path) -> str:
    """The target filename with everything but `${output-vresmode}` filled in."""
    m = re.search(wf.get("base_regex") or r"^(.*)\.[^\.]+$", source.name)
    if not m:
        raise TemplateError("base filename regex doesn't match")
    variables = {"workflow-tag": wf.get("workflow_tag", ""), "ext": OUTPUT_EXT, "source-ext": source.suffix,
                 "workflow-name": wf.get("name", "")}
    return expand(wf.get("target_format") or "$1${ext}", variables, m.groups(), keep=("output-vresmode",))


def final_name(template: str, vresmode: str) -> str:
    return safe_filename(expand(template, {"output-vresmode": vresmode}))


# ----------------------------------------------------- media probing (cached)

_probe_lock = threading.Lock()
_probe_cache: dict[str, tuple[float, int, dict]] = {}


def probe_media(path: str) -> dict:
    """{height, field_order, tags} for a media file -- container header only,
    no decoding. Cached in memory by (mtime, size)."""
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = _norm(path)
    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    info: dict = {}
    try:
        proc = subprocess.run(
            [CONFIG["tools"]["ffprobe"], "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height,field_order:format_tags", "-of", "json", path],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or [{}]
        info = {
            "height": streams[0].get("height"),
            "field_order": streams[0].get("field_order"),
            "tags": {k.upper(): v for k, v in ((data.get("format") or {}).get("tags") or {}).items()},
        }
    except (OSError, ValueError, subprocess.SubprocessError):
        info = {}
    with _probe_lock:
        _probe_cache[key] = (st.st_mtime, st.st_size, info)
    return info


def probe_many(paths: list[str]) -> dict[str, dict]:
    if not paths:
        return {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(paths, pool.map(probe_media, paths)))


def predict_vresmode(wf: dict, source: Path, info: dict) -> str:
    if wf.get("upscale_enabled"):
        try:
            preset = load_preset(resolve_workflow(wf.get("content_type") or "auto", bool(wf.get("allow_generative")))["topaz_preset"])
            return f"{preset['output_tier_height']}p"
        except (KeyError, OSError):
            return "1080p"
    height = info.get("height")
    if height:
        interlaced = (info.get("field_order") or "progressive") not in ("progressive", "unknown")
        return f"{height}{'i' if interlaced and not wf.get('deinterlace_enabled') else 'p'}"

    hint = detect_source_scan_hint(source.name)
    if hint:
        return hint[:-1] + ("p" if wf.get("deinterlace_enabled") else hint[-1])
    return "?"


def plan_outputs(paths: list[str], wf: dict, workflow_dir: str) -> list[dict]:
    """Per source: the job's resolved temp/milestone/output dirs and filename
    template, plus a predicted final path."""
    need_probe = [] if wf.get("upscale_enabled") else paths
    infos = probe_many(need_probe)
    plans = []
    for p in paths:
        src = Path(p)
        plan: dict = {"source": str(src)}
        try:
            plan["temp_dir"] = resolve_dir(wf.get("temp_dir") or "", src, workflow_dir) if wf.get("temp_dir") else None
            plan["milestone_dir"] = resolve_dir(wf.get("milestone_dir") or "", src, workflow_dir) if wf.get("milestone_dir") else None
            plan["output_dir"] = resolve_dir(wf.get("output_dir") or "${dir-source}", src, workflow_dir)
            plan["name_template"] = name_template(wf, src)
            vres = predict_vresmode(wf, src, infos.get(p, {}))
            plan["vresmode"] = vres
            plan["output_path"] = os.path.join(plan["output_dir"], final_name(plan["name_template"], vres))
        except (TemplateError, re.error) as e:
            plan["error"] = str(e)
        plans.append(plan)
    return plans


ACTIVE_STATUSES = ("pending", "running", "paused", "needs_review", "needs_restart")


def find_conflicts(plans: list[dict]) -> None:
    """Marks plans whose predicted output already exists, collides with
    another plan in the batch, or with an active queued job's prediction."""
    queued = {}
    for job in db.list_jobs():
        if job["status"] in ACTIVE_STATUSES and job["predicted_output"]:
            queued[_norm(job["predicted_output"])] = job["original_filename"]
    counts: dict[str, int] = {}
    for plan in plans:
        if plan.get("output_path"):
            counts[_norm(plan["output_path"])] = counts.get(_norm(plan["output_path"]), 0) + 1
    for plan in plans:
        out = plan.get("output_path")
        if not out:
            plan["conflict"] = plan.get("error", "invalid")
            continue
        key = _norm(out)
        if "?" in Path(out).name:
            plan["conflict"] = None
            plan["warning"] = "output resolution unknown until processed"
        if key == _norm(plan["source"]):
            plan["conflict"] = "output would overwrite the source file"
        elif os.path.exists(out):
            plan["conflict"] = "a file with this name already exists"
        elif counts[key] > 1:
            plan["conflict"] = "several selected files map to this same output"
        elif key in queued:
            plan["conflict"] = f"a queued job ({queued[key]}) is predicted to write this file"
        else:
            plan.setdefault("conflict", None)


# ------------------------------------------------------- completion status

def _tags_stages(tags: dict) -> Optional[tuple[str, ...]]:
    if tags.get("WORKFLOW_STAGES") is not None:
        return tuple(s for s in tags["WORKFLOW_STAGES"].split(",") if s)
    keys = {"deinterlace": "DEINTERLACE", "denoise": "DENOISE", "deblur": "DEBLUR",
            "dehalo": "DEHALO", "upscale": "UPSCALE"}
    if not any(k in tags for k in keys.values()):
        return None
    return tuple(s for s, k in keys.items() if tags.get(k) and tags.get(k) != "skipped")


def completion_status(paths: list[str], wf: dict, workflow_dir: str) -> dict[str, dict]:
    """{source path: {completed, via, output}} for the given workflow. Checks,
    in order: finished jobs in the manifest with the same stage set (and tag);
    processing-history metadata embedded in videos in the workflow's output
    directory (finalize.py's SOURCE_FILE / WORKFLOW_* tags -- older outputs
    are matched on their per-stage tags); then the predicted filename with
    `${output-vresmode}` as a wildcard."""
    stages = workflow_stages(wf)
    tag = wf.get("workflow_tag") or ""
    result = {p: {"completed": False} for p in paths}
    by_norm = {_norm(p): p for p in paths}

    for job in db.list_jobs("done"):
        p = by_norm.get(_norm(job["original_nas_path"]))
        if not p or result[p]["completed"] or job_stages(job) != stages:
            continue
        if job["workflow_tag"] is not None and job["workflow_tag"] != tag:
            continue
        out = job["current_file"]
        if out and os.path.exists(out):
            result[p] = {"completed": True, "via": "job", "output": out}

    plans = {}
    for p in paths:
        if result[p]["completed"]:
            continue
        src = Path(p)
        try:
            plans[p] = (resolve_dir(wf.get("output_dir") or "${dir-source}", src, workflow_dir), name_template(wf, src))
        except (TemplateError, re.error):
            continue

    listings: dict[str, list[str]] = {}
    for out_dir, _ in plans.values():
        if out_dir not in listings:
            try:
                listings[out_dir] = [e.name for e in os.scandir(out_dir) if e.is_file()]
            except OSError:
                listings[out_dir] = []

    candidates = sorted({os.path.join(d, n) for d, names in listings.items() for n in names
                         if Path(n).suffix.lower() == OUTPUT_EXT})
    meta = probe_many(candidates)
    by_source: dict[str, list[tuple[str, dict]]] = {}
    for c, info in meta.items():
        tags = info.get("tags") or {}
        if tags.get("SOURCE_FILE"):
            by_source.setdefault(tags["SOURCE_FILE"].lower(), []).append((c, tags))

    for p, (out_dir, tmpl) in plans.items():
        src = Path(p)
        for c, tags in by_source.get(src.name.lower(), []):
            if _norm(c) == _norm(p) or _norm(Path(c).parent) != _norm(out_dir):
                continue
            if tags.get("SOURCE_PATH") and _norm(tags["SOURCE_PATH"]) != _norm(p):
                continue
            if _tags_stages(tags) != stages:
                continue
            if tags.get("WORKFLOW_TAG") is not None and tags["WORKFLOW_TAG"] != tag:
                continue
            result[p] = {"completed": True, "via": "metadata", "output": c}
            break
        if result[p]["completed"]:
            continue
        marker = "VRESMODE0MARKER"
        pattern = re.escape(safe_filename(expand(tmpl, {"output-vresmode": marker}))).replace(marker, r"\d+[ip]")
        patterns = [re.compile(f"^{pattern}$", re.IGNORECASE)]
        legacy = _legacy_name_pattern(src, stages)
        if legacy:
            patterns.append(legacy)
        for n in listings.get(out_dir, []):
            c = os.path.join(out_dir, n)
            if _norm(c) == _norm(p) or not any(rx.match(n) for rx in patterns):
                continue
            # A name match only counts if the file's own processing metadata
            # (when it has any) doesn't say a different stage set made it.
            found = _tags_stages((meta.get(c) or {}).get("tags") or {})
            if found is not None and found != stages:
                continue
            result[p] = {"completed": True, "via": "filename", "output": c}
            break
    return result


def _legacy_name_pattern(src: Path, stages: tuple[str, ...]) -> Optional[re.Pattern]:
    """Name the pre-workflow pipeline gave this source's output (naming.py:
    every bracket tag dropped, then '[Upscaled 1080p]' or e.g. '[480p Dehalo]')."""

    base, _tags, _ext = split_tags(src.name)
    if "upscale" in stages:
        tag = r"Upscaled \d+p"
    else:
        extras = "".join(f" {t}" for s, t in (("denoise", "Denoised"), ("deblur", "Deblur"), ("dehalo", "Dehalo"))
                         if s in stages)
        tag = rf"\d+p{re.escape(extras)}"
    return re.compile(rf"^{re.escape(base)} \[{tag}\]\.mkv$", re.IGNORECASE)


# ------------------------------------------------------------------ listing

def list_dirs(directory: str) -> list[dict]:
    out = []
    for e in os.scandir(directory):
        try:
            if not e.is_dir():
                continue
        except OSError:
            continue
        out.append({"name": e.name, "path": str(Path(e.path)),
                    "has_settings": os.path.exists(os.path.join(e.path, SETTINGS_NAME))})
    out.sort(key=lambda d: d["name"].lower())
    return out


def list_files(directory: str, recursive: bool, limit: int = 20000) -> tuple[list[dict], bool]:
    base = Path(directory)
    files: list[dict] = []
    truncated = False

    def walk(d: Path):
        nonlocal truncated
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError:
            return
        subdirs = []
        for e in entries:
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            if is_dir:
                subdirs.append(Path(e.path))
                continue
            if e.name == SETTINGS_NAME:
                continue
            if len(files) >= limit:
                truncated = True
                return
            rel = os.path.relpath(e.path, base)
            try:
                size = e.stat().st_size
            except OSError:
                size = None
            files.append({"name": e.name, "rel": rel, "path": str(Path(e.path)), "size": size,
                          "is_video": Path(e.name).suffix.lower() in VIDEO_EXTS})
        if recursive:
            for s in subdirs:
                if truncated:
                    return
                walk(s)

    walk(base)
    return files, truncated
