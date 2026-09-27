"""Subprocess helpers shared by every stage: run a command, stream its output
into the job log table line-by-line, and raise on non-zero exit."""
from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Sequence, Union

from . import db


class CommandError(RuntimeError):
    def __init__(self, cmd: Sequence[str], returncode: int, tail: str):
        self.cmd = cmd
        self.returncode = returncode
        self.tail = tail
        super().__init__(f"Command failed ({returncode}): {' '.join(str(c) for c in cmd)}\n{tail}")


class Progress(NamedTuple):
    percent: float
    frames: Optional[int] = None        # frames done so far (on the whole job's timeline)
    total_frames: Optional[int] = None  # frames this stage will produce in total


# A progress parser takes one line of subprocess output and returns a 0-100
# percent estimate (bare float, or a Progress when it also knows frame
# counts), or None if that line carries no progress info.
ProgressParser = Callable[[str], Union[float, Progress, None]]

_FFMPEG_TIME_RE = re.compile(r"\btime=(\d+):(\d\d):(\d\d\.\d+)")
_FFMPEG_FRAME_RE = re.compile(r"\bframe=\s*(\d+)")


def _apply_progress(job_id: str, result: Union[float, Progress, None]) -> None:
    if result is None:
        return
    if isinstance(result, Progress):
        fields = {"progress_percent": round(result.percent, 1)}
        if result.frames is not None:
            fields["progress_frames"] = result.frames
        if result.total_frames is not None:
            fields["progress_total_frames"] = result.total_frames
        db.update_job(job_id, **fields)
    else:
        db.update_job(job_id, progress_percent=round(result, 1))
_HANDBRAKE_PERCENT_RE = re.compile(r"Encoding:.*?(\d+(?:\.\d+)?)\s*%")


def ffmpeg_time_progress(total_duration_seconds: Optional[float]) -> ProgressParser:
    """Parses ffmpeg's periodic `time=HH:MM:SS.ss` stats line into a percent
    of `total_duration_seconds`. Using elapsed *time* rather than frame count
    keeps this accurate even across stages that change the effective frame
    count/rate (QTGMC bobbing, IVTC decimation) -- real-time duration doesn't
    change the way frame count does.

    Frame counts come from the same line's `frame=N`; the total is
    extrapolated from ffmpeg's own output frames-per-second-of-timeline
    (frame / time), which stays correct for rate-changing stages too."""
    def parse(line: str):
        if not total_duration_seconds:
            return None
        m = _FFMPEG_TIME_RE.search(line)
        if not m:
            return None
        h, mnt, s = m.groups()
        seconds = int(h) * 3600 + int(mnt) * 60 + float(s)
        pct = max(0.0, min(99.0, seconds / total_duration_seconds * 100))
        fm = _FFMPEG_FRAME_RE.search(line)
        if not fm or seconds < 1:
            return pct
        frames = int(fm.group(1))
        total = max(frames, round(frames / seconds * total_duration_seconds))
        return Progress(pct, frames, total)
    return parse


def handbrake_percent_progress() -> ProgressParser:
    """Parses HandBrakeCLI's own `Encoding: task 1 of 1, NN.NN %` line -- it
    already reports a direct percent, no duration needed."""
    def parse(line: str) -> Optional[float]:
        m = _HANDBRAKE_PERCENT_RE.search(line)
        if not m:
            return None
        return max(0.0, min(99.0, float(m.group(1))))
    return parse


# Tracks every subprocess currently owned by the (single) worker thread, so an
# API request on a different thread (job abort) can forcefully kill whatever
# the active job is running -- there's otherwise no way to interrupt a
# subprocess.Popen.wait() from outside its own thread.
_active_procs: list[subprocess.Popen] = []
_active_procs_lock = threading.Lock()


def _track(proc: subprocess.Popen) -> None:
    with _active_procs_lock:
        _active_procs.append(proc)


def _untrack(proc: subprocess.Popen) -> None:
    with _active_procs_lock:
        if proc in _active_procs:
            _active_procs.remove(proc)


def kill_active() -> int:
    """Best-effort force-kill of every currently-tracked subprocess (used for
    job cancellation). Returns how many were signaled."""
    with _active_procs_lock:
        procs = list(_active_procs)
    killed = 0
    for p in procs:
        try:
            p.kill()
            killed += 1
        except Exception:
            pass
    return killed


# Manual "pause the whole worker" control, so the user can reclaim the GPU
# (gaming, editing) without losing in-progress generative-upscale work. Lives
# here (not worker.py) so stages/upscale.py can check it mid-job without a
# circular import -- same reason kill_active() lives here.
_pause_requested = False
_paused = False
_pause_lock = threading.Lock()


def request_pause() -> None:
    global _pause_requested
    with _pause_lock:
        _pause_requested = True


def request_resume() -> None:
    global _pause_requested, _paused
    with _pause_lock:
        _pause_requested = False
        _paused = False


def pause_state() -> str:
    """"running" | "pausing" (requested, not yet at a safe stopping point) |
    "paused" (actually stopped)."""
    with _pause_lock:
        if _paused:
            return "paused"
        if _pause_requested:
            return "pausing"
        return "running"


def should_pause_now() -> bool:
    """Checked at a safe boundary (between stages, between upscale
    checkpoint segments). If a pause was requested, latches _paused=True and
    returns True so the caller can stop cleanly and resumably."""
    global _paused
    with _pause_lock:
        if _pause_requested:
            _paused = True
            return True
        return False


def run_logged(
    job_id: str,
    stage: str,
    cmd: Sequence[str],
    cwd: Optional[Path] = None,
    input_data: Optional[bytes] = None,
    extra_env: Optional[dict] = None,
    progress: Optional[ProgressParser] = None,
) -> str:
    """Runs cmd, streaming stdout+stderr into job_logs, and returns the full
    combined output tail (last ~4000 chars) for callers that need to parse it
    (e.g. idet stats). When `progress` is given, each output line is also fed
    to it and any non-None result is written to the job's progress_* columns."""
    db.log(job_id, stage, "RUN: " + " ".join(str(c) for c in cmd))
    env = {**os.environ, **extra_env} if extra_env else None
    proc = subprocess.Popen(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    _track(proc)
    try:
        if input_data is not None:
            proc.stdin.write(input_data.decode("utf-8", errors="replace"))
            proc.stdin.close()

        lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                lines.append(line)
                db.log(job_id, stage, line)
                if progress is not None:
                    _apply_progress(job_id, progress(line))
        proc.wait()
    finally:
        _untrack(proc)

    tail = "\n".join(lines[-80:])
    if proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, tail)
    return "\n".join(lines)


def run_piped_logged(
    job_id: str,
    stage: str,
    cmd_a: Sequence[str],
    cmd_b: Sequence[str],
    progress: Optional[ProgressParser] = None,
) -> None:
    """Runs `cmd_a | cmd_b` (e.g. VSPipe | ffmpeg), streaming both processes'
    stderr into the job log, raising if either exits non-zero. `progress`, if
    given, is fed cmd_b's output lines (e.g. ffmpeg's own stats) the same way
    as run_logged."""
    db.log(job_id, stage, "RUN (piped): " + " ".join(str(c) for c in cmd_a) + "  |  " + " ".join(str(c) for c in cmd_b))
    proc_a = subprocess.Popen(
        [str(c) for c in cmd_a],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    proc_b = subprocess.Popen(
        [str(c) for c in cmd_b],
        stdin=proc_a.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc_a.stdout is not None
    proc_a.stdout.close()  # let proc_b own the read end

    _track(proc_a)
    _track(proc_b)
    try:
        lines: list[str] = []
        assert proc_b.stdout is not None
        for line in proc_b.stdout:
            line = line.rstrip("\n")
            if line:
                lines.append(line)
                db.log(job_id, stage, f"[out] {line}")
                if progress is not None:
                    _apply_progress(job_id, progress(line))

        proc_b.wait()
        proc_a.wait()
    finally:
        _untrack(proc_a)
        _untrack(proc_b)

    a_err = proc_a.stderr.read().decode("utf-8", errors="replace") if proc_a.stderr else ""
    if a_err.strip():
        for l in a_err.strip().splitlines()[-40:]:
            db.log(job_id, stage, f"[in-err] {l}")

    if proc_b.returncode != 0:
        raise CommandError(cmd_b, proc_b.returncode, "\n".join(lines[-80:]) +
                           "\nUpstream stderr:\n" + a_err[-4000:])
    if proc_a.returncode not in (0, None):
        raise CommandError(cmd_a, proc_a.returncode, a_err[-4000:])
