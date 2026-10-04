"""Loads config.json from the project root and exposes it as a plain dict.

Kept intentionally simple (no schema library) since this is a single-operator
tool and the file is meant to be hand-edited.
"""
from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
PRESETS_DIR = PROJECT_ROOT / "app" / "presets"
VPY_TEMPLATES_DIR = PROJECT_ROOT / "app" / "vpy_templates"
STATIC_DIR = PROJECT_ROOT / "app" / "static"
DB_PATH = PROJECT_ROOT / "pipeline.db"
LOGS_DIR = PROJECT_ROOT / "logs"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_preset(name: str) -> dict:
    path = PRESETS_DIR / f"{name}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_workflow(content_type: str, allow_generative: bool) -> dict:
    """content_types.json type key + the 'Allow Generative Tools' flag ->
    that type's workflow dict ({label, topaz_preset, denoise_tune}). 'auto'
    resolves to its configured fallback type -- the probe stage's detector
    replaces it per file. Raises KeyError for an unknown type."""
    ct = load_preset("content_types")
    t = ct["types"][content_type]
    if t.get("auto"):
        t = ct["types"][ct["auto_fallback"]]
    return ct["workflows"][t["generative_workflow"] if allow_generative else t["workflow"]]


_NON_TOPAZ_PRESET_FILES = {"denoise_tunes", "content_types", "deblur", "dehalo"}


def list_presets() -> list[str]:
    return sorted(p.stem for p in PRESETS_DIR.glob("*.json") if p.stem not in _NON_TOPAZ_PRESET_FILES)


CONFIG = load_config()
