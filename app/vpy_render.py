"""Renders .vpy (VapourSynth Python) scripts from Jinja2 templates, injecting
the shared plugin-path table from config.json so every template loads the
same reused-from-Hybrid plugin DLLs the same way."""
from __future__ import annotations

from jinja2 import Environment, FileSystemLoader

from .config import CONFIG, VPY_TEMPLATES_DIR

_env = Environment(
    loader=FileSystemLoader(str(VPY_TEMPLATES_DIR)),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(template_name: str, **context) -> str:
    template = _env.get_template(template_name)
    ctx = {
        "vs_plugins": CONFIG["vs_plugins"],
        "vsscripts_dir": CONFIG["tools"]["vsscripts_dir"],
        "vsfilters_support_dir": CONFIG["tools"]["vsfilters_support_dir"],
        **context,
    }
    return template.render(**ctx)
