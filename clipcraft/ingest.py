"""Get a local video file from a URL or a path on disk."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from .config import Config


def _looks_like_url(source: str) -> bool:
    return source.startswith(("http://", "https://", "www."))


def content_id(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]


def file_hash(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()[:16]


def ingest(source: str, cfg: Config) -> Path:
    """Return a local video file path for `source` (URL or existing file)."""
    local = Path(source)
    if local.exists() and local.is_file():
        return local.resolve()

    if not _looks_like_url(source):
        raise FileNotFoundError(
            f"'{source}' is neither an existing file nor a URL. "
            "Pass a local video path or a http(s) link."
        )

    dest_dir = cfg.work_dir / "downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(dest_dir / f"{content_id(source)}.%(ext)s")

    # Prefer a single progressive mp4 up to 1080p; fall back to best.
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "-o", out_tmpl,
        "--no-playlist",
        source,
    ]
    print(f"[ingest] downloading {source}")
    subprocess.run(cmd, check=True)

    hits = sorted(dest_dir.glob(f"{content_id(source)}.*"))
    videos = [h for h in hits if h.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
    if not videos:
        raise RuntimeError("yt-dlp finished but no video file was produced.")
    return videos[0]
