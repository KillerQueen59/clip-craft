"""ClipCraft GUI backend.

A thin Flask layer over the existing `clipcraft` CLI. It never re-implements the
pipeline — every heavy action shells out to `py -3 -m clipcraft ...` exactly like
the terminal does, and streams that process's stdout to the browser over SSE.
The GUI's only real state is the job registry (running/finished subprocesses).
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "projects"
STATIC_DIR = Path(__file__).resolve().parent / "static"
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi")

app = Flask(__name__, static_folder=None)


# --------------------------------------------------------------------------- #
# Job manager — one subprocess per job, stdout streamed into a ring buffer.
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self, cmd: list[str], label: str):
        self.id = uuid.uuid4().hex[:12]
        self.cmd = cmd
        self.label = label
        self.lines: list[str] = []
        self.done = False
        self.returncode: int | None = None
        self.started = time.time()
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _emit(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
            for q in list(self._subscribers):
                q.put(line)

    def _run(self) -> None:
        self._emit(f"$ {' '.join(self.cmd)}")
        try:
            self._proc = subprocess.Popen(
                self.cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:  # pragma: no cover - launch failure
            self._emit(f"[gui] failed to launch: {exc}")
            self._finish(1)
            return
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._emit(line.rstrip("\n"))
        self._proc.wait()
        self._finish(self._proc.returncode)

    def _finish(self, rc: int | None) -> None:
        self.returncode = rc
        self.done = True
        self._emit(f"[gui] __DONE__ exit={rc}")

    def cancel(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            for line in self.lines:  # replay history to a fresh subscriber
                q.put(line)
            if not self.done:
                self._subscribers.append(q)
        return q


JOBS: dict[str, Job] = {}


def start_job(cmd: list[str], label: str) -> Job:
    job = Job(cmd, label)
    JOBS[job.id] = job
    job.start()
    return job


def _py(*args: str) -> list[str]:
    """clipcraft invocation matching the CLI: `py -3 -u -m clipcraft ...`.
    `-u` keeps stdout unbuffered so logs stream live."""
    return [sys.executable, "-u", "-m", "clipcraft", *args]


# --------------------------------------------------------------------------- #
# Path safety — only ever read/serve files that live under the repo root.
# --------------------------------------------------------------------------- #
def _safe(path_str: str) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = ROOT / p
    p = p.resolve()
    if ROOT not in p.parents and p != ROOT:
        raise ValueError("path escapes project root")
    return p


# --------------------------------------------------------------------------- #
# Static frontend
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return send_file(STATIC_DIR / "index.html")


@app.get("/static/<path:name>")
def static_files(name: str):
    return send_file(STATIC_DIR / name)


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
def _project_runs(pdir: Path) -> list[dict]:
    out_dir = pdir / "output"
    runs = []
    if out_dir.is_dir():
        for m in sorted(out_dir.glob("*/clips/manifest.json")):
            run_dir = m.parent.parent
            try:
                clips = json.loads(m.read_text(encoding="utf-8"))
            except Exception:
                clips = []
            runs.append({
                "name": run_dir.name,
                "clips": len(clips),
                "mtime": m.stat().st_mtime,
            })
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return runs


@app.get("/api/projects")
def list_projects():
    PROJECTS_DIR.mkdir(exist_ok=True)
    projects = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if not d.is_dir():
            continue
        sources = [p.name for p in (d / "source").glob("*")
                   if p.suffix.lower() in VIDEO_EXTS] if (d / "source").is_dir() else []
        projects.append({
            "name": d.name,
            "sources": sources,
            "runs": _project_runs(d),
        })
    return jsonify(projects)


@app.post("/api/projects")
def create_project():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    job = start_job(_py("init", name), f"init {name}")
    return jsonify({"job": job.id})


@app.get("/api/projects/<name>")
def get_project(name: str):
    pdir = PROJECTS_DIR / name
    if not pdir.is_dir():
        return jsonify({"error": "not found"}), 404
    cfg = pdir / "config.toml"
    brief = pdir / "brief.txt"
    return jsonify({
        "name": name,
        "config": cfg.read_text(encoding="utf-8") if cfg.exists() else "",
        "brief": brief.read_text(encoding="utf-8") if brief.exists() else "",
        "sources": [p.name for p in (pdir / "source").glob("*")
                    if p.suffix.lower() in VIDEO_EXTS] if (pdir / "source").is_dir() else [],
        "runs": _project_runs(pdir),
    })


@app.put("/api/projects/<name>/config")
def save_config(name: str):
    pdir = PROJECTS_DIR / name
    if not pdir.is_dir():
        return jsonify({"error": "not found"}), 404
    (pdir / "config.toml").write_text((request.json or {}).get("text", ""), encoding="utf-8")
    return jsonify({"ok": True})


@app.put("/api/projects/<name>/brief")
def save_brief(name: str):
    pdir = PROJECTS_DIR / name
    if not pdir.is_dir():
        return jsonify({"error": "not found"}), 404
    (pdir / "brief.txt").write_text((request.json or {}).get("text", ""), encoding="utf-8")
    return jsonify({"ok": True})


@app.post("/api/projects/<name>/import-source")
def import_source(name: str):
    """Copy a local video file into the project's source/ folder so it becomes a
    first-class source and recut can auto-find it."""
    pdir = PROJECTS_DIR / name
    if not pdir.is_dir():
        return jsonify({"error": "not found"}), 404
    src = (request.json or {}).get("path", "").strip()
    p = Path(src)
    if not p.exists() or p.suffix.lower() not in VIDEO_EXTS:
        return jsonify({"error": "not a video file"}), 400
    dest_dir = pdir / "source"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / p.name
    if dest.resolve() != p.resolve():
        shutil.copy2(p, dest)
    return jsonify({"ok": True, "name": p.name})


# --------------------------------------------------------------------------- #
# Runs / candidates / manifests
# --------------------------------------------------------------------------- #
@app.get("/api/projects/<name>/runs/<run>")
def get_run(name: str, run: str):
    run_dir = PROJECTS_DIR / name / "output" / run
    manifest = run_dir / "clips" / "manifest.json"
    if not manifest.exists():
        return jsonify({"error": "no manifest"}), 404
    clips = json.loads(manifest.read_text(encoding="utf-8"))
    for c in clips:
        outs = c.get("outputs") or {}
        c["_files"] = {fmt: str(Path(path).relative_to(ROOT))
                       for fmt, path in outs.items() if Path(path).exists()}
        thumb = c.get("thumbnail")
        if thumb and Path(thumb).exists():
            c["_thumb"] = str(Path(thumb).relative_to(ROOT))
    return jsonify({"name": run, "clips": clips})


@app.get("/api/file")
def serve_file():
    """Serve a rendered clip / thumbnail by repo-relative path (range-enabled by
    send_file for smooth video scrubbing)."""
    try:
        p = _safe(request.args["path"])
    except (KeyError, ValueError):
        return "bad path", 400
    if not p.exists():
        return "not found", 404
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    return send_file(p, mimetype=mime, conditional=True)


@app.post("/api/reveal")
def reveal():
    """Open a folder in Explorer, or reveal a single file (Explorer /select)."""
    d = request.json or {}
    try:
        p = _safe(d.get("path", ""))
    except ValueError:
        return jsonify({"error": "bad path"}), 400
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    try:
        if p.is_dir():
            os.startfile(str(p))  # noqa: S606 - local, path-guarded
        else:
            subprocess.Popen(["explorer", "/select,", str(p)])
    except Exception as exc:  # pragma: no cover
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Pipeline actions (all stream to a job)
# --------------------------------------------------------------------------- #
@app.post("/api/run")
def api_run():
    d = request.json or {}
    project, source = d.get("project"), d.get("source", "").strip()
    if not project or not source:
        return jsonify({"error": "project and source required"}), 400
    args = ["run", "--project", project, "--source", source]
    if d.get("name"):
        args += ["--name", d["name"].strip()]
    if d.get("n"):
        args += ["--n", str(int(d["n"]))]
    if d.get("render_top"):
        args += ["--render-top", str(int(d["render_top"]))]
    if d.get("formats"):
        args += ["--formats", d["formats"].strip()]
    if d.get("brief"):
        args += ["--brief", d["brief"]]
    job = start_job(_py(*args), f"run {project}")
    return jsonify({"job": job.id})


@app.post("/api/candidates")
def api_candidates():
    d = request.json or {}
    project, source = d.get("project"), d.get("source", "").strip()
    if not project or not source:
        return jsonify({"error": "project and source required"}), 400
    args = ["candidates", "--project", project, "--source", source]
    if d.get("name"):
        args += ["--name", d["name"].strip()]
    if d.get("n"):
        args += ["--n", str(int(d["n"]))]
    if d.get("brief"):
        args += ["--brief", d["brief"]]
    job = start_job(_py(*args), f"candidates {project}")
    return jsonify({"job": job.id})


@app.post("/api/doctor")
def api_doctor():
    d = request.json or {}
    args = ["doctor"]
    if d.get("project"):
        args += ["--project", d["project"]]
    job = start_job(_py(*args), "doctor")
    return jsonify({"job": job.id})


# --------------------------------------------------------------------------- #
# Recut — read/write the editable transcript sidecar, then re-render.
# --------------------------------------------------------------------------- #
@app.get("/api/recut/sidecar")
def recut_sidecar():
    name = request.args["project"]
    run = request.args["run"]
    clip = int(request.args["clip"])
    sidecar = PROJECTS_DIR / name / "output" / run / "clips" / f"clip_{clip:02d}.words.json"
    if not sidecar.exists():
        return jsonify({"exists": False, "words": []})
    return jsonify({"exists": True, "words": json.loads(sidecar.read_text(encoding="utf-8"))})


@app.put("/api/recut/sidecar")
def recut_sidecar_save():
    d = request.json or {}
    name, run, clip = d["project"], d["run"], int(d["clip"])
    sidecar = PROJECTS_DIR / name / "output" / run / "clips" / f"clip_{clip:02d}.words.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(d.get("words", []), ensure_ascii=False, indent=2),
                       encoding="utf-8")
    return jsonify({"ok": True})


@app.post("/api/recut")
def api_recut():
    d = request.json or {}
    project, run, clip = d.get("project"), d.get("run"), d.get("clip")
    if not project or clip is None:
        return jsonify({"error": "project and clip required"}), 400
    args = ["recut", "--project", project, "--clip", str(int(clip))]
    if run:
        args += ["--name", run]
    if d.get("formats"):
        args += ["--formats", d["formats"].strip()]
    if d.get("source"):
        args += ["--source", d["source"].strip()]
    job = start_job(_py(*args), f"recut {project} #{clip}")
    return jsonify({"job": job.id})


# --------------------------------------------------------------------------- #
# Upload — YouTube + TikTok
# --------------------------------------------------------------------------- #
@app.post("/api/upload")
def api_upload():
    d = request.json or {}
    project, run = d.get("project"), d.get("run")
    if not project or not run:
        return jsonify({"error": "project and run required"}), 400
    args = ["upload", "--project", project, "--name", run]
    if d.get("format"):
        args += ["--format", d["format"]]
    if d.get("privacy"):
        args += ["--privacy", d["privacy"]]
    if d.get("every"):
        args += ["--every", str(int(d["every"]))]
    if d.get("account"):
        args += ["--account", d["account"]]
    if d.get("clips"):
        args += ["--clips", d["clips"].strip()]
    if d.get("dry_run"):
        args += ["--dry-run"]
    job = start_job(_py(*args), f"upload {project}")
    return jsonify({"job": job.id})


@app.post("/api/tiktok")
def api_tiktok():
    d = request.json or {}
    project = d.get("project")
    if not project:
        return jsonify({"error": "project required"}), 400
    args = ["tiktok", "--project", project]
    if d.get("run"):
        args += ["--name", d["run"]]
    if d.get("format"):
        args += ["--format", d["format"]]
    if d.get("auth"):
        args += ["--auth"]
    if d.get("code"):
        args += ["--code", d["code"].strip()]
    if d.get("dry_run"):
        args += ["--dry-run"]
    job = start_job(_py(*args), f"tiktok {project}")
    return jsonify({"job": job.id})


# --------------------------------------------------------------------------- #
# Job status + live log stream
# --------------------------------------------------------------------------- #
@app.get("/api/jobs/<job_id>")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({"id": job.id, "label": job.label, "done": job.done,
                    "returncode": job.returncode, "lines": job.lines})


@app.post("/api/jobs/<job_id>/cancel")
def job_cancel(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    job.cancel()
    return jsonify({"ok": True})


@app.get("/api/jobs/<job_id>/stream")
def job_stream(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return "unknown job", 404

    def gen():
        q = job.subscribe()
        # If already finished, replay was queued by subscribe(); drain and stop.
        while True:
            try:
                line = q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                if job.done:
                    break
                continue
            yield f"data: {json.dumps(line)}\n\n"
            if line.startswith("[gui] __DONE__"):
                break

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def create_app() -> Flask:
    return app


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, threaded=True)
