"""Subprocess helpers shared by every stage: run a command, stream its output
into the job log table line-by-line, and raise on non-zero exit."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional, Sequence

from . import db


class CommandError(RuntimeError):
    def __init__(self, cmd: Sequence[str], returncode: int, tail: str):
        self.cmd = cmd
        self.returncode = returncode
        self.tail = tail
        super().__init__(f"Command failed ({returncode}): {' '.join(str(c) for c in cmd)}\n{tail}")


def run_logged(
    job_id: str,
    stage: str,
    cmd: Sequence[str],
    cwd: Optional[Path] = None,
    input_data: Optional[bytes] = None,
    extra_env: Optional[dict] = None,
) -> str:
    """Runs cmd, streaming stdout+stderr into job_logs, and returns the full
    combined output tail (last ~4000 chars) for callers that need to parse it
    (e.g. idet stats)."""
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
    proc.wait()

    tail = "\n".join(lines[-80:])
    if proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, tail)
    return "\n".join(lines)


def run_piped_logged(
    job_id: str,
    stage: str,
    cmd_a: Sequence[str],
    cmd_b: Sequence[str],
) -> None:
    """Runs `cmd_a | cmd_b` (e.g. VSPipe | ffmpeg), streaming both processes'
    stderr into the job log, raising if either exits non-zero."""
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

    lines: list[str] = []
    assert proc_b.stdout is not None
    for line in proc_b.stdout:
        line = line.rstrip("\n")
        if line:
            lines.append(line)
            db.log(job_id, stage, f"[out] {line}")

    proc_b.wait()
    proc_a.wait()

    a_err = proc_a.stderr.read().decode("utf-8", errors="replace") if proc_a.stderr else ""
    if a_err.strip():
        for l in a_err.strip().splitlines()[-40:]:
            db.log(job_id, stage, f"[in-err] {l}")

    if proc_a.returncode not in (0, None):
        raise CommandError(cmd_a, proc_a.returncode, a_err[-4000:])
    if proc_b.returncode != 0:
        raise CommandError(cmd_b, proc_b.returncode, "\n".join(lines[-80:]))
