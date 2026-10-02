"""Speaker-overlap decision layer — WHEN to show one face vs. both.

The question "are two people talking at once?" is answerable from the mixed
audio track alone, independent of the camera. We run pyannote speaker
diarization over a clip window; the diarization already resolves overlapping
speech, so `Annotation.get_overlap()` gives the spans where 2+ people talk
simultaneously. Everything else is a single active speaker.

`mode_timeline()` turns that into a smoothed list of spans:

    [{"start": 0.0, "end": 4.2, "mode": "single", "speaker": "SPEAKER_01"},
     {"start": 4.2, "end": 6.1, "mode": "duo",    "speaker": None}, ...]

clip-local seconds. `mode == "duo"` -> show the duo stack (both people);
`mode == "single"` -> focus that one speaker. This is only the *decision*; where
to actually crop each speaker is the framing problem (per-source, see
docs/stack-layouts.md) and lives elsewhere.

Standalone check:  py -3 -m clipcraft.diarize <video> <start> <end> [config.toml]

pyannote's models are gated: needs a free HuggingFace token (env HF_TOKEN /
HUGGINGFACE_TOKEN / HUGGING_FACE_HUB_TOKEN, or [diarize].hf_token in config) plus
a one-time "Agree" on the pyannote/speaker-diarization-3.1 model page.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import Config
from .ingest import file_hash

_SR = 16000  # pyannote is trained at 16 kHz mono


class DiarizeUnavailable(RuntimeError):
    """pyannote.audio / torch is not installed."""


class HFTokenMissing(RuntimeError):
    """No HuggingFace token available to download the gated pyannote model."""


def _token(cfg: Config) -> str:
    tok = (cfg.raw.get("diarize", {}).get("hf_token", "")
           or os.environ.get("HF_TOKEN", "")
           or os.environ.get("HUGGINGFACE_TOKEN", "")
           or os.environ.get("HUGGING_FACE_HUB_TOKEN", ""))
    if not tok:
        raise HFTokenMissing(
            "No HuggingFace token. Create one at "
            "https://huggingface.co/settings/tokens, accept the conditions on "
            "https://huggingface.co/pyannote/speaker-diarization-3.1, then set "
            "HF_TOKEN or [diarize].hf_token in config.")
    return tok


def _extract_wav(video: Path, start: float, end: float, cfg: Config,
                 cache_dir: Path, key: str) -> Path:
    """Mono 16 kHz WAV of just the clip window, cached."""
    out = cache_dir / f"{key}.wav"
    if out.exists():
        return out
    if not cfg.ffmpeg:
        raise RuntimeError("ffmpeg not found. Run `python -m clipcraft doctor`.")
    cmd = [cfg.ffmpeg, "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
           "-i", str(video), "-vn", "-ac", "1", "-ar", str(_SR),
           "-c:a", "pcm_s16le", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


_PIPELINE = None  # loaded once per process; the model load is the slow part


def _pipeline(cfg: Config):
    global _PIPELINE
    if _PIPELINE is not None:
        return _PIPELINE
    try:
        import torch
        from pyannote.audio import Pipeline
    except ImportError as e:
        raise DiarizeUnavailable(
            "pyannote.audio not installed. `py -3 -m pip install pyannote.audio`."
        ) from e
    dconf = cfg.raw.get("diarize", {})
    model = dconf.get("model", "pyannote/speaker-diarization-3.1")
    pipe = Pipeline.from_pretrained(model, token=_token(cfg))
    if pipe is None:
        raise HFTokenMissing(
            f"pyannote returned no pipeline for '{model}'. The token is likely "
            "valid but you haven't accepted the model conditions on its HF page.")
    device = dconf.get("device", "cuda" if torch.cuda.is_available() else "cpu")
    pipe.to(torch.device(device))
    _PIPELINE = pipe
    return pipe


def _load_waveform(wav: Path):
    """Decode the mono PCM wav into a (1, samples) float32 tensor ourselves.

    pyannote's newer io path reads files through torchcodec, whose FFmpeg DLLs
    don't load on this Windows box. Passing an in-memory {"waveform",
    "sample_rate"} dict makes pyannote skip the decoder entirely (io.py:328/384).
    """
    import wave

    import numpy as np
    import torch

    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return torch.from_numpy(data).unsqueeze(0), sr


def _raw_spans(video: Path, start: float, end: float, cfg: Config):
    """Run diarization; return (dur, [(t0, t1, speaker_or_None_for_overlap)])
    in clip-local seconds, wall-to-wall with no gaps unmarked."""
    dconf = cfg.raw.get("diarize", {})
    cache_dir = cfg.work_dir / "diarize"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = f"{file_hash(video)}_{start:.2f}_{end:.2f}"
    wav = _extract_wav(video, start, end, cfg, cache_dir, key)

    pipe = _pipeline(cfg)
    hint = {}
    if dconf.get("num_speakers"):
        hint["num_speakers"] = int(dconf["num_speakers"])
    else:
        if dconf.get("min_speakers"):
            hint["min_speakers"] = int(dconf["min_speakers"])
        if dconf.get("max_speakers"):
            hint["max_speakers"] = int(dconf["max_speakers"])
    waveform, sr = _load_waveform(wav)
    diar = pipe({"waveform": waveform, "sample_rate": sr, "uri": key}, **hint)
    # Newer pyannote wraps the Annotation in a DiarizeOutput dataclass.
    diar = getattr(diar, "speaker_diarization", diar)

    dur = round(end - start, 3)
    overlap = diar.get_overlap()  # Timeline of 2+ concurrent speakers

    # Sample on a fine grid: at each instant, is it overlap, else which speaker,
    # else silence. Overlap and silence both have no single speaker, but they must
    # NOT collapse together: overlap == duo (frame between people), silence == a
    # pause where we hold on whoever last spoke. Track them apart.
    step = float(dconf.get("grid", 0.10))
    n = max(1, int(round(dur / step)))
    marks: list[list] = []                # [t, spk_or_None, is_overlap]
    for i in range(n):
        t = i * step
        if any(seg.start <= t < seg.end for seg in overlap):
            marks.append([t, None, True])  # None == overlap/duo
            continue
        spk = None
        for seg, _, label in diar.itertracks(yield_label=True):
            if seg.start <= t < seg.end:
                spk = label
                break
        marks.append([t, spk, False])      # spk None here == silence, not duo

    # Silence inherits the adjacent single speaker so a stall holds on a person
    # instead of framing the dead centre between two seats. Carry the last real
    # speaker forward across silence (overlap does not reset the hold); then
    # back-fill any leading silence from the first speaker that follows.
    last_single = None
    for m in marks:
        if m[2]:
            continue
        if m[1] is not None:
            last_single = m[1]
        elif last_single is not None:
            m[1] = last_single
    next_single = None
    for m in reversed(marks):
        if m[2]:
            continue
        if m[1] is not None:
            next_single = m[1]
        elif next_single is not None:
            m[1] = next_single

    runs: list[list] = []
    for t, spk, _ov in marks:
        if runs and runs[-1][2] == spk:
            runs[-1][1] = t + step
        else:
            runs.append([t, t + step, spk])
    if runs:
        runs[-1][1] = dur
    return dur, [(round(a, 3), round(b, 3), s) for a, b, s in runs]


def mode_timeline(video: Path, start: float, end: float,
                  cfg: Config) -> list[dict]:
    """Smoothed [{start, end, mode, speaker}] in clip-local seconds.

    Raw diarization flickers; we (1) drop overlap bursts shorter than
    `min_duo`, (2) drop single-speaker slivers shorter than `min_single`
    (absorbed into the neighbour), (3) merge adjacent runs of the same
    (mode, speaker), and (4) drop a trailing speaker to bridge tiny gaps of
    the same speaker across a hiccup. A None speaker means duo."""
    dconf = cfg.raw.get("diarize", {})
    min_duo = float(dconf.get("min_duo", 0.8))
    min_single = float(dconf.get("min_single", 1.5))

    dur, runs = _raw_spans(video, start, end, cfg)
    runs = [list(r) for r in runs]  # _raw_spans returns tuples; smoothing mutates

    def mode_of(spk):
        return "duo" if spk is None else "single"

    # 1+2: drop too-short runs by folding into the longer neighbour.
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i, (a, b, s) in enumerate(runs):
            length = b - a
            floor = min_duo if s is None else min_single
            if length >= floor:
                continue
            left = runs[i - 1] if i > 0 else None
            right = runs[i + 1] if i + 1 < len(runs) else None
            if left and (not right or (left[1] - left[0]) >= (right[1] - right[0])):
                left[1] = b
            elif right:
                right[0] = a
            else:
                break
            del runs[i]
            changed = True
            break

    # 3: merge adjacent same-key runs.
    merged: list[list] = []
    for a, b, s in runs:
        if merged and merged[-1][2] == s:
            merged[-1][1] = b
        else:
            merged.append([a, b, s])

    return [{"start": round(a, 3), "end": round(b, 3),
             "mode": mode_of(s), "speaker": s} for a, b, s in merged]


def duo_spans(timeline: list[dict]) -> list[tuple[float, float]]:
    """Just the (start, end) spans where mode == duo — for overlay gating."""
    return [(d["start"], d["end"]) for d in timeline if d["mode"] == "duo"]


if __name__ == "__main__":
    import sys
    from . import config as _cfg

    if len(sys.argv) < 4:
        print("usage: py -3 -m clipcraft.diarize <video> <start> <end> [config]")
        raise SystemExit(2)
    vid = Path(sys.argv[1])
    s, e = float(sys.argv[2]), float(sys.argv[3])
    cpath = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    c = _cfg.load(cpath)
    tl = mode_timeline(vid, s, e, c)
    print(f"clip {s:.2f}-{e:.2f}s ({e - s:.1f}s):")
    for d in tl:
        who = d["speaker"] or "BOTH"
        print(f"  {d['start']:6.2f} - {d['end']:6.2f}  {d['mode']:6s}  {who}")
    dur = e - s
    duo = sum(b - a for a, b in duo_spans(tl))
    print(f"duo coverage: {duo:.1f}s / {dur:.1f}s ({100 * duo / dur:.0f}%)")
