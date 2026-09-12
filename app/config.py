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


_NON_TOPAZ_PRESET_FILES = {"denoise_tunes", "content_types"}


def list_presets() -> list[str]:
    return sorted(p.stem for p in PRESETS_DIR.glob("*.json") if p.stem not in _NON_TOPAZ_PRESET_FILES)


CONFIG = load_config()
