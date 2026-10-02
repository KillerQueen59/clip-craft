#!/usr/bin/env python3
"""Unattended clip generation for the Clip System.

Watches each project's ``inbox/`` for new source videos, runs the full
clipcraft pipeline on the machine that actually has the GPU/Ollama/ffmpeg,
and parks finished clips in a review queue for a human to fix titles and
post. It never posts anything itself -- the title + review + publish gates
stay in your hands (see docs/auto-runner.md).

Layout it expects / maintains, per project under ``projects/<name>/``::

    inbox/            <- drop source videos here (watched)
    inbox/_failed/    <- a source whose run errored is moved here
    source/           <- processed sources are moved here (never reprocessed)
    output/<job>/     <- finished clips (existing clipcraft output)
    output/<job>/_REVIEW.txt   <- the `review` digest for that job
    _REVIEW_QUEUE.md  <- running list of jobs awaiting your title-fix + post

Two ways to run it (wire both via Task Scheduler -- see docs):

    python auto_runner.py --watch         # loop, instant pickup (run at logon)
    python auto_runner.py --once          # one sweep then exit (nightly safety net)
    python auto_runner.py --once --project "Pojok Jenaka"   # limit to one project
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECTS_DIR = ROOT / "projects"
LOCKS_DIR = ROOT / "_runner" / "locks"
LOG_DIR = ROOT / "_runner" / "logs"

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".ts", ".flv"}
STALE_LOCK_HOURS = 6  # a lock older than this is treated as abandoned


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    line = f"[{_now()}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    day = _dt.date.today().isoformat()
    with (LOG_DIR / f"auto_runner_{day}.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _project_dirs(only: str | None) -> list[Path]:
    if not PROJECTS_DIR.is_dir():
        return []
    if only:
        p = PROJECTS_DIR / only
        return [p] if p.is_dir() else []
    out = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if d.is_dir() and not d.name.startswith((".", "_")):
            out.append(d)
    return out


def _inbox_videos(project: Path) -> list[Path]:
    inbox = project / "inbox"
    if not inbox.is_dir():
        return []
    vids = []
    for f in sorted(inbox.iterdir()):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS and not f.name.startswith("."):
            vids.append(f)
    return vids


def _acquire_lock(project: Path) -> Path | None:
    """Atomic per-project lock (mkdir). Returns the lock path or None if busy."""
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCKS_DIR / (project.name + ".lock")
    if lock.exists():
        age_h = (time.time() - lock.stat().st_mtime) / 3600.0
        if age_h < STALE_LOCK_HOURS:
            return None
        _log(f"lock for '{project.name}' is stale ({age_h:.1f}h) -> reclaiming")
        try:
            shutil.rmtree(lock)
        except OSError:
            return None
    try:
        lock.mkdir()  # atomic create; fails if another process just took it
    except FileExistsError:
        return None
    (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")
    return lock


def _release_lock(lock: Path | None) -> None:
    if lock is not None:
        shutil.rmtree(lock, ignore_errors=True)


def _run_clipcraft(project: Path, source: Path, job_name: str, logfh) -> int:
    cmd = [
        sys.executable, "-m", "clipcraft", "run",
        "--project", project.name,
        "--source", str(source),
        "--name", job_name,
    ]
    logfh.write(f"\n$ {' '.join(cmd)}\n")
    logfh.flush()
    proc = subprocess.run(cmd, cwd=str(ROOT), stdout=logfh, stderr=subprocess.STDOUT)
    return proc.returncode


def _write_review(project: Path, job_name: str) -> str:
    """Run `review` for the job and stash the digest; return its text."""
    cmd = [
        sys.executable, "-m", "clipcraft", "review",
        "--project", project.name, "--name", job_name,
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    digest = proc.stdout or ""
    job_dir = project / "output" / job_name
    if job_dir.is_dir():
        (job_dir / "_REVIEW.txt").write_text(digest, encoding="utf-8")
    return digest


def _enqueue_review(project: Path, job_name: str) -> None:
    q = project / "_REVIEW_QUEUE.md"
    header = "# Review queue -- fix titles, then post\n\n"
    if not q.exists():
        q.write_text(header, encoding="utf-8")
    with q.open("a", encoding="utf-8") as fh:
        fh.write(
            f"- [ ] **{job_name}** -- generated {_now()} -- "
            f"`output/{job_name}/_REVIEW.txt` "
            f"(fix titles -> `recut`, then `upload`/`tiktok`/`telegram`)\n"
        )


def _process_one(project: Path, source: Path) -> bool:
    job_name = source.stem
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    run_log = LOG_DIR / f"{project.name}__{job_name}.log"
    _log(f"START  '{project.name}' <- {source.name}  (job='{job_name}')")
    with run_log.open("a", encoding="utf-8") as logfh:
        logfh.write(f"\n===== {_now()}  {project.name} / {job_name} =====\n")
        rc = _run_clipcraft(project, source, job_name, logfh)

    if rc != 0:
        failed = project / "inbox" / "_failed"
        failed.mkdir(parents=True, exist_ok=True)
        dest = failed / source.name
        shutil.move(str(source), str(dest))
        _log(f"FAILED '{project.name}' rc={rc}  -> moved to inbox/_failed/  "
             f"(see {run_log.name})")
        return False

    # Move the processed source out of inbox so it is never picked up again.
    src_dir = project / "source"
    src_dir.mkdir(parents=True, exist_ok=True)
    dest = src_dir / source.name
    if dest.exists():
        dest = src_dir / f"{source.stem}__{int(time.time())}{source.suffix}"
    shutil.move(str(source), str(dest))

    _write_review(project, job_name)
    _enqueue_review(project, job_name)
    _log(f"DONE   '{project.name}' / {job_name}  -> queued for review "
         f"(_REVIEW_QUEUE.md)")
    return True


def sweep(only: str | None) -> int:
    processed = 0
    for project in _project_dirs(only):
        vids = _inbox_videos(project)
        if not vids:
            continue
        lock = _acquire_lock(project)
        if lock is None:
            _log(f"SKIP   '{project.name}' is locked by another run")
            continue
        try:
            for source in _inbox_videos(project):  # re-read: inbox may have grown
                if _process_one(project, source):
                    processed += 1
        finally:
            _release_lock(lock)
    return processed


def main() -> int:
    ap = argparse.ArgumentParser(description="Unattended clipcraft runner.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true",
                      help="do one sweep of all inboxes, then exit")
    mode.add_argument("--watch", action="store_true",
                      help="loop forever, sweeping every --interval seconds")
    ap.add_argument("--interval", type=int, default=30,
                    help="seconds between sweeps in --watch mode (default 30)")
    ap.add_argument("--project", default=None,
                    help="limit to a single project folder name")
    args = ap.parse_args()

    if args.once:
        n = sweep(args.project)
        if n:  # stay quiet on the every-minute no-op sweeps
            _log(f"sweep complete: processed {n} source(s)")
        return 0

    _log(f"watching {PROJECTS_DIR}  (interval {args.interval}s)  "
         f"project={args.project or 'ALL'}")
    while True:
        try:
            sweep(args.project)
        except Exception as e:  # never let the watcher die on one bad file
            _log(f"sweep error: {e!r}")
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
