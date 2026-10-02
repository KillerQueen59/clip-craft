"""clipcraft HTTP API — a thin orchestration surface over the clipcraft CLI.

Everything heavy (whisper, selection, ffmpeg render, uploads) still runs in the
clipcraft package exactly as the CLI runs it; this service only shells out to it,
tracks the job, and exposes the JSON artifacts (candidates.json, manifest.json)
the CLI already writes to disk. Intended for n8n and the Discord bot to call.

Run from the repo root:
    uvicorn integrations.api.app:app --host 0.0.0.0 --port 8756
"""

from __future__ import annotations

import os
import re
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .jobs import JobRunner

ROOT = Path(__file__).resolve().parents[2]        # repo root: "Clip System"
PROJECTS_DIR = ROOT / "projects"

# The interpreter that has clipcraft's deps installed. Override if the API runs
# under a different Python than clipcraft (e.g. a venv). Space-separated, so
# "py -3" or "C:\\path\\to\\python.exe" both work.
CLIPCRAFT_PYTHON = os.environ.get("CLIPCRAFT_PYTHON", "py -3").strip()
API_TOKEN = os.environ.get("CLIPCRAFT_API_TOKEN", "").strip()

runner = JobRunner(ROOT / "integrations" / ".jobs")
app = FastAPI(title="clipcraft API", version="1.0.0")


def _slug(text: str) -> str:
    """Mirror of clipcraft.cli._slug so we can locate run folders by name."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", text.strip()).strip("-.")
    return s[:60] or "clip"


def _python() -> list[str]:
    # Whitespace split (not shlex) so Windows paths keep their backslashes.
    return CLIPCRAFT_PYTHON.split() + ["-m", "clipcraft"]


def _project_output(project: str) -> Path:
    pdir = PROJECTS_DIR / project
    if not pdir.exists():
        raise HTTPException(404, f"project not found: {project}")
    return pdir / "output"


def _run_dir(project: str, name: str) -> Path:
    d = _project_output(project) / _slug(name)
    if not d.exists():
        raise HTTPException(404, f"run not found: {project}/{_slug(name)}")
    return d


def _check_auth(token: str | None) -> None:
    # Optional shared-secret gate. n8n/bot send it as the `x-token` query param.
    if API_TOKEN and token != API_TOKEN:
        raise HTTPException(401, "bad or missing token")


# --------------------------------------------------------------------------- #
# request bodies
# --------------------------------------------------------------------------- #
class RunReq(BaseModel):
    project: str
    source: str
    n: int = 10
    render_top: int = 0
    formats: str | None = None
    brief: str | None = None
    name: str | None = None
    webhook_url: str | None = Field(
        None, description="n8n URL to POST the job result to when it finishes")


class CandidatesReq(BaseModel):
    project: str
    source: str
    n: int = 10
    brief: str | None = None
    name: str | None = None
    webhook_url: str | None = None


class UploadReq(BaseModel):
    project: str
    name: str
    format: str = "mobile"
    privacy: str = "unlisted"
    every: int = 60
    account: str | None = None
    clips: str | None = None
    dry_run: bool = False
    webhook_url: str | None = None


class TikTokReq(BaseModel):
    project: str
    name: str
    format: str = "mobile"
    dry_run: bool = False
    webhook_url: str | None = None


class RecutReq(BaseModel):
    project: str
    name: str
    clip: int
    formats: str | None = None
    source: str | None = None
    webhook_url: str | None = None


class TitleReq(BaseModel):
    title: str


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"ok": True, "root": str(ROOT), "python": CLIPCRAFT_PYTHON}


@app.get("/projects")
def list_projects(x_token: str | None = None) -> dict:
    _check_auth(x_token)
    if not PROJECTS_DIR.exists():
        return {"projects": []}
    return {"projects": sorted(p.name for p in PROJECTS_DIR.iterdir()
                               if p.is_dir())}


@app.get("/projects/{project}/runs")
def list_runs(project: str, x_token: str | None = None) -> dict:
    _check_auth(x_token)
    out = _project_output(project)
    if not out.exists():
        return {"runs": []}
    runs = []
    for d in sorted(out.iterdir()):
        if (d / "clips" / "manifest.json").exists():
            runs.append(d.name)
    return {"runs": runs}


@app.get("/projects/{project}/runs/{name}/candidates")
def get_candidates(project: str, name: str, x_token: str | None = None) -> list:
    _check_auth(x_token)
    f = _run_dir(project, name) / "candidates.json"
    if not f.exists():
        raise HTTPException(404, "candidates.json not found")
    return json.loads(f.read_text(encoding="utf-8"))


@app.get("/projects/{project}/runs/{name}/manifest")
def get_manifest(project: str, name: str, x_token: str | None = None) -> list:
    _check_auth(x_token)
    f = _run_dir(project, name) / "clips" / "manifest.json"
    if not f.exists():
        raise HTTPException(404, "manifest.json not found")
    return json.loads(f.read_text(encoding="utf-8"))


@app.patch("/projects/{project}/runs/{name}/clips/{clip}")
def edit_title(project: str, name: str, clip: int, body: TitleReq,
               x_token: str | None = None) -> dict:
    """Rewrite one clip's title in the manifest. Call /jobs/recut afterwards to
    re-burn the title card onto the video."""
    _check_auth(x_token)
    mpath = _run_dir(project, name) / "clips" / "manifest.json"
    if not mpath.exists():
        raise HTTPException(404, "manifest.json not found")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    entry = next((c for c in manifest if c.get("clip") == clip), None)
    if entry is None:
        raise HTTPException(404, f"clip {clip} not in manifest")
    entry["title"] = body.title
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return {"ok": True, "clip": clip, "title": body.title}


@app.get("/file")
def get_file(path: str, x_token: str | None = None) -> FileResponse:
    """Serve a rendered clip/thumbnail. Restricted to files under projects/ so
    this cannot be used to read arbitrary disk paths."""
    _check_auth(x_token)
    p = Path(path).resolve()
    if PROJECTS_DIR.resolve() not in p.parents:
        raise HTTPException(403, "path outside projects/")
    if not p.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(str(p))


@app.get("/jobs/{job_id}")
def job_status(job_id: str, x_token: str | None = None) -> dict:
    _check_auth(x_token)
    job = runner.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.public()


@app.post("/jobs/candidates")
def start_candidates(req: CandidatesReq, x_token: str | None = None) -> dict:
    _check_auth(x_token)
    name = req.name or req.source
    cmd = _python() + ["candidates", "--project", req.project,
                       "--source", req.source, "--n", str(req.n)]
    if req.brief:
        cmd += ["--brief", req.brief]
    if req.name:
        cmd += ["--name", req.name]
    # Only the --name path is deterministic; without it the CLI derives the run
    # folder from the source, so we can't locate candidates.json to attach.
    result = (_project_output(req.project) / _slug(name) / "candidates.json"
              if req.name else None)
    job = runner.start(cmd, ROOT, result_file=result,
                       webhook_url=req.webhook_url)
    return job.public()


@app.post("/jobs/run")
def start_run(req: RunReq, x_token: str | None = None) -> dict:
    _check_auth(x_token)
    name = req.name or req.source
    cmd = _python() + ["run", "--project", req.project,
                       "--source", req.source, "--n", str(req.n)]
    if req.render_top:
        cmd += ["--render-top", str(req.render_top)]
    if req.formats:
        cmd += ["--formats", req.formats]
    if req.brief:
        cmd += ["--brief", req.brief]
    if req.name:
        cmd += ["--name", req.name]
    # See note in start_candidates: manifest is locatable only when --name is set.
    result = (_project_output(req.project) / _slug(name) / "clips" / "manifest.json"
              if req.name else None)
    job = runner.start(cmd, ROOT, result_file=result,
                       webhook_url=req.webhook_url)
    return job.public()


@app.post("/jobs/recut")
def start_recut(req: RecutReq, x_token: str | None = None) -> dict:
    _check_auth(x_token)
    cmd = _python() + ["recut", "--project", req.project,
                       "--name", req.name, "--clip", str(req.clip)]
    if req.formats:
        cmd += ["--formats", req.formats]
    if req.source:
        cmd += ["--source", req.source]
    result = _run_dir(req.project, req.name) / "clips" / "manifest.json"
    job = runner.start(cmd, ROOT, result_file=result,
                       webhook_url=req.webhook_url)
    return job.public()


@app.post("/jobs/upload")
def start_upload(req: UploadReq, x_token: str | None = None) -> dict:
    _check_auth(x_token)
    cmd = _python() + ["upload", "--project", req.project, "--name", req.name,
                       "--format", req.format, "--privacy", req.privacy,
                       "--every", str(req.every)]
    if req.account:
        cmd += ["--account", req.account]
    if req.clips:
        cmd += ["--clips", req.clips]
    if req.dry_run:
        cmd += ["--dry-run"]
    job = runner.start(cmd, ROOT, webhook_url=req.webhook_url)
    return job.public()


@app.post("/jobs/tiktok")
def start_tiktok(req: TikTokReq, x_token: str | None = None) -> dict:
    _check_auth(x_token)
    cmd = _python() + ["tiktok", "--project", req.project, "--name", req.name,
                       "--format", req.format]
    if req.dry_run:
        cmd += ["--dry-run"]
    job = runner.start(cmd, ROOT, webhook_url=req.webhook_url)
    return job.public()
