"""In-memory background-job runner for the clipcraft API.

Each job shells out to the clipcraft CLI in a worker thread, streams combined
stdout+stderr to a log file, and optionally POSTs its result to an n8n webhook
when it finishes. Single-machine, single-user: an in-process dict is enough —
we do not persist jobs across restarts.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Job:
    id: str
    cmd: list[str]
    status: str = "running"          # running | done | failed
    returncode: int | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    log_path: Path | None = None
    result: dict | None = None       # parsed artifact (candidates/manifest), when known
    error: str | None = None
    _tail: deque = field(default_factory=lambda: deque(maxlen=200))

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "returncode": self.returncode,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "cmd": self.cmd,
            "log_tail": list(self._tail),
            "result": self.result,
            "error": self.error,
        }


class JobRunner:
    def __init__(self, log_dir: Path):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def start(self, cmd: list[str], cwd: Path,
              result_file: Path | None = None,
              webhook_url: str | None = None) -> Job:
        """Launch `cmd` in a worker thread. When it exits, parse `result_file`
        (if given) into job.result and POST the job's public state to
        `webhook_url` (if given)."""
        job = Job(id=uuid.uuid4().hex[:12], cmd=cmd)
        job.log_path = self.log_dir / f"{job.id}.log"
        with self._lock:
            self._jobs[job.id] = job
        t = threading.Thread(
            target=self._run, args=(job, cmd, cwd, result_file, webhook_url),
            daemon=True)
        t.start()
        return job

    def _run(self, job: Job, cmd: list[str], cwd: Path,
             result_file: Path | None, webhook_url: str | None) -> None:
        try:
            with open(job.log_path, "w", encoding="utf-8", errors="replace") as log:
                log.write(f"$ {' '.join(shlex.quote(c) for c in cmd)}\n\n")
                log.flush()
                proc = subprocess.Popen(
                    cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", bufsize=1)
                for line in proc.stdout:  # type: ignore[union-attr]
                    log.write(line)
                    log.flush()
                    job._tail.append(line.rstrip("\n"))
                proc.wait()
                job.returncode = proc.returncode
            job.status = "done" if job.returncode == 0 else "failed"
            if result_file and result_file.exists():
                try:
                    job.result = json.loads(result_file.read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    job.error = f"could not parse {result_file.name}: {exc}"
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
        finally:
            job.finished_at = time.time()
            if webhook_url:
                self._notify(webhook_url, job)

    @staticmethod
    def _notify(url: str, job: Job) -> None:
        try:
            import requests
            requests.post(url, json=job.public(), timeout=15)
        except Exception:  # noqa: BLE001 — a dead webhook must not fail the job
            pass
