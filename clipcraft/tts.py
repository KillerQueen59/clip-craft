"""Hook voiceover via edge-tts (free, no API key).

Reads the clip's hook (or title) aloud over the opening seconds and mixes it in,
ducking the original audio underneath so the synthetic voice stays clear.

edge-tts needs internet (it calls Microsoft's servers); it is "free" in the sense
of no key and no per-character cost, not offline. On any failure we warn and skip
the voiceover rather than break the render.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .config import Config


def available() -> bool:
    """True if the edge_tts module is importable in this interpreter."""
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


def _speechify(text: str) -> str:
    """Smooth a headline for reading aloud. Card titles are punchy on screen but
    make the voice halt: edge-tts inserts a ~1s pause on '?'/'!' and pauses around
    quotes. Soften terminal punctuation to a short comma-pause and drop quote marks
    so the read flows. The on-screen card is unaffected (it uses clip['title'])."""
    for q in ('"', "'", "“", "”", "‘", "’"):
        text = text.replace(q, "")
    for p in ("?", "!"):
        text = text.replace(p, ",")
    text = text.replace("—", ",").replace(" - ", ", ")
    while ",," in text:
        text = text.replace(",,", ",")
    return " ".join(text.split()).strip(" ,")


def _voice_text(clip: dict, source: str) -> str:
    if source == "title":
        raw = clip.get("title") or clip.get("hook") or ""
    else:
        raw = clip.get("hook") or clip.get("title") or ""
    return _speechify(raw.strip())


def synthesize(text: str, out_path: Path, cfg: Config) -> Path | None:
    """Render `text` to an mp3 at out_path via edge-tts. Returns the path, or None
    on failure (missing module, no network, empty text)."""
    text = (text or "").strip()
    if not text:
        return None
    tcfg = cfg.raw.get("tts", {})
    voice = tcfg.get("voice", "id-ID-GadisNeural")
    rate = str(tcfg.get("rate", "+0%"))
    volume = str(tcfg.get("volume", "+0%"))
    cmd = [
        sys.executable, "-m", "edge_tts",
        "--voice", voice,
        # `--opt=val` form: a negative value like "-15%" is otherwise parsed as a flag.
        f"--rate={rate}", f"--volume={volume}",
        "--text", text, "--write-media", str(out_path),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-8:])
        print(f"[tts] voiceover synth failed for voice={voice}; skipping.\n{tail}")
        return None
    return out_path


def _probe_duration(ffprobe: str, path: Path) -> float:
    if not ffprobe:
        return 0.0
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace")
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def apply_hook_voiceover(video: Path, clip: dict, cfg: Config,
                         work: Path) -> None:
    """Add a hook voiceover to an already-rendered clip, in place.

    Two modes, chosen by `[tts] hold_first_frame`:

    - True (default): freeze the opening frame while the voice reads the hook,
      then let the clip roll with its own audio. Adds `voice + lead_in` seconds
      to the front; video is re-encoded (tpad needs it).
    - False: talk over the moving video from t=0, ducking the original audio for
      the voiceover's duration. Video is stream-copied (no re-encode).

    Silently no-ops if disabled, unavailable, or the synth fails.
    """
    tcfg = cfg.raw.get("tts", {})
    if not tcfg.get("enabled", False):
        return
    if not available():
        print("[tts] edge-tts not installed — run `pip install edge-tts`. "
              "Skipping voiceover.")
        return
    text = _voice_text(clip, tcfg.get("source", "hook"))
    if not text:
        return

    voice_mp3 = work / (video.stem + ".voice.mp3")
    if synthesize(text, voice_mp3, cfg) is None:
        return
    vdur = _probe_duration(cfg.ffprobe, voice_mp3)
    if vdur <= 0:
        return

    lead_in = float(tcfg.get("lead_in", 0.15) or 0.0)
    duck = float(tcfg.get("duck", 0.35))
    gain = float(tcfg.get("gain", 1.0))
    hold = bool(tcfg.get("hold_first_frame", True))
    lead_ms = int(round(lead_in * 1000))
    window = round(vdur + lead_in, 3)
    window_ms = int(round(window * 1000))

    tmp_out = work / (video.stem + ".voiced.mp4")
    if hold:
        # Freeze frame 0 for `window`s (voice plays, original audio held back),
        # then the clip resumes with full audio. The held frame is blurred so the
        # opening reads as a teaser, then snaps sharp when the clip starts.
        sigma = float(tcfg.get("blur_sigma", 24) or 0)
        blur = (f",gblur=sigma={sigma}:enable='lt(t,{window:.3f})'"
                if sigma > 0 else "")
        fc = (
            f"[0:v]tpad=start_duration={window:.3f}:start_mode=clone{blur}[outv];"
            f"[0:a]adelay={window_ms}:all=1[a];"
            f"[1:a]adelay={lead_ms}:all=1,volume={gain}[vo];"
            "[a][vo]amix=inputs=2:duration=longest:dropout_transition=0:"
            "normalize=0[outa]"
        )
        vcodec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                  "-pix_fmt", "yuv420p"]
    else:
        # Talk over moving video, ducking the original under the voice.
        fc = (
            f"[0:a]volume={duck}:enable='lt(t,{window})'[bg];"
            f"[1:a]adelay={lead_ms}:all=1,volume={gain}[vo];"
            "[bg][vo]amix=inputs=2:duration=first:dropout_transition=0:"
            "normalize=0[outa]"
        )
        vcodec = ["-c:v", "copy"]
    cmd = [
        cfg.ffmpeg, "-y", "-i", str(video), "-i", str(voice_mp3),
        "-filter_complex", fc,
        "-map", "[outv]" if hold else "0:v", "-map", "[outa]",
    ] + vcodec + [
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp_out),
    ]
    proc = subprocess.run(cmd, cwd=work, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not tmp_out.exists():
        tail = "\n".join((proc.stdout or "").splitlines()[-15:])
        print(f"[tts] voiceover mix failed; keeping clean clip.\n{tail}")
        tmp_out.unlink(missing_ok=True)
        return
    tmp_out.replace(video)
    voice_mp3.unlink(missing_ok=True)
    how = f"freeze-hold {window:.1f}s" if hold else "talk-over"
    print(f"[tts] hook voiceover added ({how}, voice {vdur:.1f}s): \"{text}\"")
