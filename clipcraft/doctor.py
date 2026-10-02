"""Environment check with actionable errors."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from .config import Config


def _ok(msg): print(f"  OK   {msg}")
def _warn(msg): print(f"  WARN {msg}")
def _fail(msg): print(f"  FAIL {msg}")


def _check_ffmpeg(cfg: Config) -> bool:
    if cfg.ffmpeg and Path(cfg.ffmpeg).exists():
        _ok(f"ffmpeg  -> {cfg.ffmpeg}")
        if cfg.ffprobe:
            _ok(f"ffprobe -> {cfg.ffprobe}")
        else:
            _warn("ffprobe not found (probing disabled, non-fatal)")
        return True
    _fail("ffmpeg not found. Install: winget install Gyan.FFmpeg  (then re-run)")
    return False


def _check_python_deps() -> bool:
    ok = True
    for mod, hint in [("yt_dlp", "pip install yt-dlp"),
                      ("faster_whisper", "pip install faster-whisper")]:
        try:
            __import__(mod)
            _ok(f"python: {mod}")
        except Exception:
            _fail(f"python module '{mod}' missing. {hint}")
            ok = False
    return ok


def _check_cuda() -> None:
    try:
        from .transcribe import _enable_cuda_dlls
        _enable_cuda_dlls()
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        if n > 0:
            _ok(f"CUDA available: {n} GPU(s) -> whisper will use float16")
        else:
            _warn("no CUDA GPU visible to CTranslate2 -> whisper runs on CPU (slower)")
    except Exception as exc:
        _warn(f"CUDA check skipped: {exc}")


def _check_tts(cfg: Config) -> None:
    tcfg = cfg.raw.get("tts", {})
    if not tcfg.get("enabled", False):
        _ok("tts: hook voiceover disabled (skip)")
        return
    try:
        import edge_tts  # noqa: F401
        _ok(f"tts: edge-tts present, voice '{tcfg.get('voice', 'id-ID-GadisNeural')}'")
    except Exception:
        _warn("tts enabled but edge-tts missing. Run: pip install edge-tts "
              "(needs internet at render time)")


def _check_ollama(cfg: Config) -> bool:
    url = cfg.select.get("ollama_url", "http://localhost:11434").rstrip("/")
    want = cfg.select.get("model", "llama3.1:8b")
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode())
        names = [m["name"] for m in tags.get("models", [])]
        if any(n == want or n.startswith(want.split(":")[0]) for n in names):
            _ok(f"ollama up, model '{want}' present")
            return True
        _warn(f"ollama up but '{want}' not pulled. Run: ollama pull {want}")
        _warn(f"       available: {', '.join(names) or '(none)'}")
        return False
    except Exception:
        _fail("ollama not reachable at " + url + ". Start Ollama, then re-run.")
        return False


def run(cfg: Config) -> int:
    print(f"ClipCraft doctor  (python {sys.version.split()[0]})")
    print("-" * 48)
    results = [
        _check_ffmpeg(cfg),
        _check_python_deps(),
        _check_ollama(cfg),
    ]
    _check_cuda()
    _check_tts(cfg)
    print("-" * 48)
    if all(results):
        print("All required checks passed. You're ready:")
        print('  python -m clipcraft run --source "<url-or-file>" --n 10')
        return 0
    print("Some required checks failed — fix the FAIL lines above and re-run.")
    return 1
