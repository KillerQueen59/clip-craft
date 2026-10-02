"""Audience-reaction scoring — does the moment actually LAND?

The transcript tells you what was said; it cannot tell you whether it was
funny. On a podcast the other people in the room are the laugh track, so we
detect their reaction directly from the audio with PANNs (a pretrained
AudioSet CNN that recognises Laughter / Giggle / Chuckle / Cheering as
first-class sound events). We build one reaction timeline over the whole
source, cache it, then score each candidate window against it.

A funny clip is one whose payoff coincides with a spike in this timeline. The
selection layer (transcript-only, blind to delivery) proposes; this layer
re-ranks by evidence that the room reacted, and can snap the out-point onto
the laugh so the clip does not end a beat early.

Standalone check:  py -3 -m clipcraft.reaction <video-or-wav>
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from .config import Config
from .ingest import file_hash

# AudioSet classes that mean "the room reacted". Matched by substring against
# the PANNs label list so we do not hardcode indices that could drift.
_LAUGH_TERMS = ("laughter", "giggle", "chuckle", "chortle", "snicker",
                "belly laugh")
# Crowd approval — weaker signal (also fires on applause games / music), so it
# is mixed in at a discount rather than trusted outright.
_CHEER_TERMS = ("cheering", "applause", "crowd")

_SR = 32000          # PANNs sample rate — non-negotiable, the model expects it
_WIN_S = 10.0        # inference window; PANNs was trained on 10s segments


class PannsUnavailable(RuntimeError):
    """panns_inference / torch is not installed."""


def _class_indices(labels: list[str]) -> tuple[list[int], list[int]]:
    laugh, cheer = [], []
    for i, name in enumerate(labels):
        low = name.lower()
        if any(t in low for t in _LAUGH_TERMS):
            laugh.append(i)
        elif any(t in low for t in _CHEER_TERMS):
            cheer.append(i)
    return laugh, cheer


def _extract_wav(video: Path, cfg: Config, cache_dir: Path, key: str) -> Path:
    """Mono 32 kHz WAV for PANNs, cached. No filters: reaction lives in the raw
    dynamics, and loudnorm would flatten the very spikes we want to detect."""
    out = cache_dir / f"{key}.wav"
    if out.exists():
        return out
    if not cfg.ffmpeg:
        raise RuntimeError("ffmpeg not found. Run `python -m clipcraft doctor`.")
    cmd = [cfg.ffmpeg, "-y", "-i", str(video), "-vn", "-ac", "1",
           "-ar", str(_SR), "-c:a", "pcm_s16le", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def _read_wav(path: Path) -> np.ndarray:
    import wave
    with wave.open(str(path), "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() == 2:
            x = x.reshape(-1, 2).mean(axis=1)
    return x


def _run_sed(audio: np.ndarray, laugh_idx: list[int], cheer_idx: list[int],
             device: str) -> np.ndarray:
    """Framewise reaction probability for the whole audio, one row per frame.

    Windows the audio at 10s (PANNs' training length), runs the sound-event
    model per window, keeps the max over the laughter classes plus a discounted
    max over the cheer classes, and concatenates. Column 0 = laugh, 1 = cheer.
    """
    from panns_inference import SoundEventDetection

    sed = SoundEventDetection(checkpoint_path=None, device=device)
    win = int(_WIN_S * _SR)
    rows = []
    for i in range(0, len(audio), win):
        seg = audio[i:i + win]
        if len(seg) < _SR // 2:  # ignore a sub-0.5s tail
            break
        fw = sed.inference(seg[None, :])          # (1, frames, 527)
        fw = np.asarray(fw)[0]
        laugh = fw[:, laugh_idx].max(axis=1) if laugh_idx else np.zeros(len(fw))
        cheer = fw[:, cheer_idx].max(axis=1) if cheer_idx else np.zeros(len(fw))
        rows.append(np.stack([laugh, cheer], axis=1))
    if not rows:
        return np.zeros((0, 2), dtype=np.float32)
    return np.concatenate(rows, axis=0).astype(np.float32)


def reaction_timeline(video: Path, cfg: Config) -> dict:
    """{"fps": float, "duration": float, "laugh": 1-D array, "cheer": 1-D array}
    for the whole source. Cached to work/reaction/<hash>.npz."""
    rconf = cfg.raw.get("reaction", {})
    cache_dir = cfg.work_dir / "reaction"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = file_hash(video)
    npz = cache_dir / f"{key}.npz"
    if npz.exists():
        d = np.load(npz)
        return {"fps": float(d["fps"]), "duration": float(d["duration"]),
                "laugh": d["laugh"], "cheer": d["cheer"]}

    try:
        from panns_inference import labels  # noqa: F401
    except Exception as exc:  # torch or panns missing
        raise PannsUnavailable(
            f"panns_inference/torch not importable ({exc}). Install with:\n"
            "  py -3 -m pip install torch --index-url "
            "https://download.pytorch.org/whl/cpu\n"
            "  py -3 -m pip install panns_inference"
        ) from exc
    from panns_inference import labels

    laugh_idx, cheer_idx = _class_indices(list(labels))
    wav = _extract_wav(video, cfg, cache_dir, key)
    audio = _read_wav(wav)
    duration = len(audio) / _SR
    device = rconf.get("device", "cpu")
    print(f"[reaction] scoring {duration/60:.1f} min of audio with PANNs "
          f"({len(laugh_idx)} laugh classes, device={device})")
    probs = _run_sed(audio, laugh_idx, cheer_idx, device)
    fps = (len(probs) / duration) if duration else 0.0
    laugh = probs[:, 0] if len(probs) else np.zeros(0, np.float32)
    cheer = probs[:, 1] if len(probs) else np.zeros(0, np.float32)
    np.savez(npz, fps=np.float32(fps), duration=np.float32(duration),
             laugh=laugh, cheer=cheer)
    if rconf.get("keep_wav", False) is False:
        try:
            wav.unlink()
        except OSError:
            pass
    return {"fps": fps, "duration": duration, "laugh": laugh, "cheer": cheer}


def _combined(tl: dict, cfg: Config) -> np.ndarray:
    w_cheer = float(cfg.raw.get("reaction", {}).get("cheer_weight", 0.5))
    return np.clip(tl["laugh"] + w_cheer * tl["cheer"], 0.0, 1.0)


def score_window(tl: dict, start: float, end: float,
                 cfg: Config) -> tuple[float, float, float]:
    """Reaction inside [start, end]: returns
       (peak, coverage, laugh_peak_time)
    peak     — strongest reaction probability in the window (0-1)
    coverage — fraction of the window that is above the reaction threshold
    laugh_peak_time — absolute seconds of the strongest reaction (or -1)."""
    fps = tl["fps"]
    sig = _combined(tl, cfg)
    if fps <= 0 or len(sig) == 0:
        return 0.0, 0.0, -1.0
    a = max(0, int(start * fps))
    b = min(len(sig), int(end * fps))
    if b <= a:
        return 0.0, 0.0, -1.0
    seg = sig[a:b]
    thr = float(cfg.raw.get("reaction", {}).get("threshold", 0.20))
    peak = float(seg.max())
    coverage = float((seg >= thr).mean())
    peak_t = (a + int(seg.argmax())) / fps
    return peak, coverage, peak_t


def laugh_out_point(tl: dict, start: float, end: float,
                    cfg: Config) -> float:
    """Where to END the clip so it lands ON the laugh, not a beat before it.

    The LLM sets `end` on the last spoken word, so the punchline's laugh often
    falls just after it and gets cut. We search a short lookahead past `end`
    for a reaction burst that overlaps or follows the current end, and return
    the time just after that burst finishes. Returns -1 if there is no laugh to
    catch (leave the snapped end alone). Never trims — only extends."""
    rconf = cfg.raw.get("reaction", {})
    fps = tl["fps"]
    sig = _combined(tl, cfg)
    if fps <= 0 or len(sig) == 0:
        return -1.0
    thr = float(rconf.get("threshold", 0.20))
    look = float(rconf.get("out_lookahead", 4.0))
    tail = float(rconf.get("laugh_tail", 0.6))
    # Only consider a burst that touches the tail of the clip (from end-1s on),
    # so we extend for the punchline's own laugh, not some unrelated later one.
    a = max(0, int((end - 1.0) * fps))
    b = min(len(sig), int((end + look) * fps))
    if b <= a:
        return -1.0
    region = sig[a:b]
    hot = np.where(region >= thr)[0]
    if len(hot) == 0:
        return -1.0
    burst_end_t = (a + int(hot[-1])) / fps
    return burst_end_t + tail


def annotate(cands: list[dict], video: Path, cfg: Config) -> list[dict]:
    """Attach reaction evidence to each candidate and re-rank.

    Adds per candidate:
      reaction        0-1 blended peak+coverage score
      reaction_peak   strongest reaction probability in the clip
      laugh_peak      absolute seconds of the biggest laugh (or -1)
    Then folds it into `score` by `reaction_boost` (default 3.0 points) so the
    LLM's transcript score and the audio evidence both count. Optionally drops
    candidates with no reaction when [reaction] require=true (comedy channels).
    """
    if not cands:
        return cands
    rconf = cfg.raw.get("reaction", {})
    if not rconf.get("enabled", True):
        return cands
    try:
        tl = reaction_timeline(video, cfg)
    except PannsUnavailable as exc:
        print(f"[reaction] skipped: {exc}")
        return cands

    boost = float(rconf.get("reaction_boost", 2.5))
    require = bool(rconf.get("require", False))
    min_react = float(rconf.get("min_reaction", 0.15))
    # Cache the timeline on the candidate list so snap_to_laugh (post-snap) can
    # reuse it without re-loading — annotate is always called first.
    kept = []
    for c in cands:
        peak, cov, peak_t = score_window(tl, c["start"], c["end"], cfg)
        react = float(np.clip(0.7 * peak + 0.3 * min(1.0, cov * 3), 0.0, 1.0))
        c["reaction"] = round(react, 3)
        c["reaction_peak"] = round(peak, 3)
        c["laugh_peak"] = round(peak_t, 2) if peak_t >= 0 else -1
        c["_laugh_out"] = round(laugh_out_point(tl, c["start"], c["end"], cfg), 2)
        c["score"] = round(float(c.get("score", 0)) + boost * react, 3)
        if require and react < min_react:
            continue
        kept.append(c)
    kept.sort(key=lambda x: x.get("score", 0), reverse=True)
    dropped = len(cands) - len(kept)
    print(f"[reaction] scored {len(cands)} candidates"
          + (f", dropped {dropped} with no reaction" if dropped else "")
          + f" (top reaction={max((c['reaction'] for c in kept), default=0):.2f})")
    return kept


def snap_to_laugh(cands: list[dict], cfg: Config) -> list[dict]:
    """Extend each clip's end to land ON its punchline laugh. Runs AFTER
    snap_all, because word/sentence snapping would otherwise pull the end back
    off the (non-speech) laugh. Only ever extends, never trims, and never past
    the length ceiling. Uses `_laugh_out` stamped by annotate()."""
    rconf = cfg.raw.get("reaction", {})
    if not rconf.get("enabled", True) or not rconf.get("snap_out", True):
        for c in cands:
            c.pop("_laugh_out", None)
        return cands
    # Laugh-snap only ever EXTENDS the out-point; it must still honor the content
    # cap (house rule: content span <= max_seconds, hard-ceilinged at 90s). A laugh
    # that lands past the cap is not reachable — the clip ends at its snapped
    # boundary instead of overrunning to catch the reaction.
    max_s = min(float(cfg.clip.get("max_seconds", 60)), 90.0)
    ceil = max_s
    snapped = 0
    for c in cands:
        out = c.pop("_laugh_out", -1)
        if out is None or out < 0:
            continue
        # Only extend, and only within a sane ceiling from the clip start.
        if out > c["end"] + 0.05 and (out - c["start"]) <= ceil:
            c["end"] = round(out, 2)
            c["duration"] = round(c["end"] - c["start"], 2)
            snapped += 1
    if snapped:
        print(f"[reaction] extended {snapped} clip end(s) to land on the laugh")
    return cands


def _main(argv: list[str]) -> int:
    from . import config
    if len(argv) < 2:
        print("usage: py -3 -m clipcraft.reaction <video-or-wav>")
        return 2
    cfg = config.load()
    video = Path(argv[1])
    tl = reaction_timeline(video, cfg)
    sig = _combined(tl, cfg)
    fps = tl["fps"]
    print(f"duration={tl['duration']:.1f}s  fps={fps:.1f}  frames={len(sig)}")
    # 1-second bins for a readable timeline.
    if fps > 0 and len(sig):
        n = int(tl["duration"]) + 1
        for s in range(n):
            a, b = int(s * fps), int((s + 1) * fps)
            v = float(sig[a:b].max()) if b > a else 0.0
            bar = "#" * int(v * 40)
            flag = "  <== LAUGH" if v >= 0.5 else ""
            print(f"{s:5d}s {v:5.2f}  {bar}{flag}")
    overall = float(np.sort(sig)[-max(1, len(sig)//20):].mean()) if len(sig) else 0.0
    print(f"\noverall reaction (top-5% mean) = {overall:.3f}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
