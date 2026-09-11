"""Stage 5: move the finished master back to the source's NAS folder under
its demangled, fully-tagged name, then reclaim local scratch space. The
original source file is never touched or deleted -- it's archival and may be
reprocessed as tools improve."""
from __future__ import annotations

import shutil
from pathlib import Path

from .. import db
from ..naming import split_tags, join_tags

STAGE = "finalized"


def _unique_destination(dest: Path) -> Path:
    if not dest.exists():
        return dest
    base, tags, ext = split_tags(dest.name)
    n = 2
    while True:
        candidate = dest.with_name(join_tags(f"{base} ({n})", tags, ext))
        if not candidate.exists():
            return candidate
        n += 1


def run(job_id: str) -> None:
    job = db.get_job(job_id)
    settings = db.get_settings(job_id)
    current = Path(job["current_file"])

    display_name = settings.get("display_filename") or job["original_filename"]
    base, tags, _old_ext = split_tags(display_name)
    final_name = join_tags(base, tags, current.suffix)  # use the actual produced container ext

    dest_dir = Path(job["original_nas_path"]).parent
    dest_path = _unique_destination(dest_dir / final_name)

    db.log(job_id, STAGE, f"moving {current} -> {dest_path}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(current), str(dest_path))

    staging_dir = job["staging_dir"]
    if staging_dir and Path(staging_dir).exists():
        db.log(job_id, STAGE, f"cleaning up staging dir {staging_dir}")
        shutil.rmtree(staging_dir, ignore_errors=True)

    db.log(job_id, STAGE, f"finalized: {dest_path}")
    db.update_job(job_id, stage=STAGE, status="done", current_file=str(dest_path))
