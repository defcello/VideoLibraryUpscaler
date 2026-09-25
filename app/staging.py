"""Reclaim scratch files at stage boundaries without breaking stage retries."""
from pathlib import Path

from . import db
from .config import CONFIG


def cleanup(job_id: str) -> None:
    """Keep the committed stage input/output until delivery; never touch checkpoints.

    Call only when the job has no active stage subprocess. Cleanup is best effort,
    but failures are logged so a locked file cannot silently consume disk forever.
    """
    job = db.get_job(job_id)
    if job is None or not job["staging_dir"]:
        return
    try:
        directory = Path(job["staging_dir"]).resolve()
        roots = [(Path(drive) / CONFIG["staging_subdir"]).resolve()
                 for drive in CONFIG["staging_drives"]]
        if directory.name != job_id or directory.parent not in roots:
            raise ValueError(f"refusing cleanup outside configured job staging directories: {directory}")
        if not directory.exists():
            return
        protected = {Path(job["original_nas_path"]).resolve()}
        if job["status"] not in ("done", "cancelled"):
            if job["current_file"]:
                current = Path(job["current_file"]).resolve()
                if not current.is_file():
                    raise ValueError(f"retaining staging because retry input is missing: {current}")
                protected.add(current)
            elif job["stage"] != "queued":
                raise ValueError("retaining staging because retry input is unknown")
        settings = db.get_settings(job_id)
        for segment in settings.get("checkpoint_completed_segments") or []:
            protected.add(Path(segment["path"]).resolve())
        checkpoint = settings.get("checkpoint_dir")
        checkpoint = Path(checkpoint).resolve() if checkpoint else None
        reclaimed = 0
        # Do not follow directory links/junctions or delete anything outside this
        # exact job directory, even if a staging entry resolves elsewhere.
        for path in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            resolved = path.resolve()
            if not resolved.is_relative_to(directory) or resolved in protected:
                continue
            if checkpoint and (resolved == checkpoint or resolved.is_relative_to(checkpoint)):
                continue
            try:
                if path.is_file():
                    size = path.stat().st_size
                    path.unlink()
                    reclaimed += size
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError as exc:
                db.log(job_id, "cleanup", f"WARNING: could not remove {path}: {exc}")
        if not any(directory.iterdir()):
            directory.rmdir()
        if reclaimed:
            db.log(job_id, "cleanup", f"reclaimed {reclaimed / 1024**3:.2f} GiB of staging files")
    except (OSError, ValueError) as exc:
        db.log(job_id, "cleanup", f"WARNING: staging cleanup incomplete: {exc}")


def cleanup_finished_jobs() -> None:
    """Retry cleanup of retained records before the worker starts."""
    for job in db.list_jobs():
        if job["status"] in ("done", "failed", "cancelled"):
            cleanup(job["id"])
