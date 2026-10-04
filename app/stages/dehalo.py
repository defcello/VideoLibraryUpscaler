"""Stage (optional) "Remove Halo": Topaz Video AI Artemis (Strong Halo) pass
run at 1:1 (no resolution change) after Remove Blur and before upscale, to
remove ringing/halo artifacts from oversharpened DVD sources.

This used to be a two-pass Iris -> Artemis chain; the Iris pass now lives in
its own "Remove Blur" stage (deblur.py) so blurry sources with no haloing can
be cleaned up without running the dehalo model. Enabling both Remove Blur and
Remove Halo reproduces the original chain exactly. See tvai_cleanup.py for
the shared runner and app/presets/dehalo.json for the captured parameters.

Independent of the other toggles; final filename tagging and metadata
embedding both happen in finalize.py. No-op passthrough when
`job.dehalo_enabled` is unset.
"""
from __future__ import annotations

from .. import db, naming
from .tvai_cleanup import run_cleanup

STAGE = "dehaloed"


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    run_cleanup(job_id, stage=STAGE, name="dehalo", enabled=bool(job["dehalo_enabled"]),
                preset_name="dehalo", tag_fn=naming.add_dehalo_tag)
