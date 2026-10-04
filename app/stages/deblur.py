"""Stage (optional) "Remove Blur": Topaz Video AI Iris (Medium quality,
automatic parameter estimation) pass run at 1:1 (no resolution change)
between denoise and dehalo, to recover detail in soft/blurry sources.

Split out of the original Iris -> Artemis "Dehalo" chain so blurry video with
no haloing can be cleaned up without the dehalo model's side effects. See
tvai_cleanup.py for the shared runner and app/presets/deblur.json for the
captured parameters.

Independent of the other toggles; final filename tagging and metadata
embedding both happen in finalize.py. No-op passthrough when
`job.deblur_enabled` is unset.
"""
from __future__ import annotations

from .. import db, naming
from .tvai_cleanup import run_cleanup

STAGE = "deblurred"


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    run_cleanup(job_id, stage=STAGE, name="deblur", enabled=bool(job["deblur_enabled"]),
                preset_name="deblur", tag_fn=naming.add_deblur_tag)
