"""Word-level transcription via faster-whisper, cached by file hash.

Word timestamps are non-negotiable: clip boundaries live between words, not
between whisper's 30-second chunks.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .config import Config
from .ingest import file_hash


def _enable_cuda_dlls() -> None:
    """Make the pip-installed NVIDIA cuBLAS/cuDNN DLLs discoverable on Windows.

    CTranslate2 loads cuBLAS lazily via LoadLibrary, which searches the process
    PATH — so we both add the DLL directories and prepend them to PATH for the
    current process only (never the global environment)."""
    if os.name != "nt":
        return
    try:
        import nvidia
    except Exception:
        return
    base = Path(list(nvidia.__path__)[0])
    bindirs = [d for d in glob.glob(str(base / "*" / "bin")) if Path(d).is_dir()]
    if not bindirs:
        return
    for bindir in bindirs:
        try:
            os.add_dll_directory(bindir)
        except (OSError, FileNotFoundError):
            pass
    os.environ["PATH"] = os.pathsep.join(bindirs + [os.environ.get("PATH", "")])


def _pick_device(requested: str, compute_override: str = "") -> tuple[str, str]:
    """Return (device, compute_type).

    On GPU we default to int8_float16: on an 8GB Turing card it runs large-v3
    ~1.5-2x faster than float16 and lighter on VRAM, with negligible quality
    loss. Override per config with [transcribe] compute_type."""
    if requested == "cpu":
        return "cpu", (compute_override or "int8")
    # requested in ("auto", "cuda")
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", (compute_override or "int8_float16")
    except Exception:
        pass
    if requested == "cuda":
        print("[transcribe] CUDA requested but unavailable; using CPU.")
    return "cpu", (compute_override or "int8")


def _free_ollama_vram(cfg: Config) -> None:
    """Ask Ollama to evict its resident model so large-v3 has the whole GPU.

    Ollama keeps the select model resident (keep_alive) between runs; on an 8GB
    card that plus a large whisper model can OOM. A generate call with
    keep_alive:0 unloads it immediately. Best-effort — never fatal."""
    base = cfg.select.get("ollama_url", "http://localhost:11434").rstrip("/")
    model = cfg.select.get("model", "")
    if not model:
        return
    payload = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/generate", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            print(f"[transcribe] freed Ollama VRAM (unloaded {model})")
    except (urllib.error.URLError, OSError):
        pass  # Ollama not running / already unloaded — nothing to free.


def _probe_duration(video: Path, cfg: Config) -> float:
    """Duration in seconds via ffprobe, or 0.0 if it can't be determined."""
    if not cfg.ffprobe:
        return 0.0
    cmd = [cfg.ffprobe, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(video)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                             encoding="utf-8", errors="replace")
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return 0.0


def _clean_audio(video: Path, cfg: Config, cache_dir: Path, sig: str) -> Path | None:
    """Extract a denoised/normalized 16 kHz mono WAV for cleaner recognition.

    Audio filters never shift timing, so word timestamps stay valid against the
    original video. Returns the wav path, or None to transcribe the video
    directly (feature disabled or ffmpeg missing)."""
    aconf = cfg.transcribe.get("audio", {})
    if not aconf.get("clean", True) or not cfg.ffmpeg:
        return None
    filters = []
    hp = aconf.get("highpass", 80)
    if hp:
        filters.append(f"highpass=f={int(hp)}")
    if aconf.get("denoise", False):
        filters.append("afftdn=nf=-25")
    if aconf.get("loudnorm", True):
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    if not filters:
        return None

    out = cache_dir / f"{sig}.wav"
    if out.exists():
        return out
    cmd = [cfg.ffmpeg, "-y", "-i", str(video), "-vn",
           "-af", ",".join(filters), "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", str(out)]
    print(f"[transcribe] cleaning audio ({', '.join(filters)})")
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return out
    except subprocess.CalledProcessError as exc:
        print(f"[transcribe] audio clean failed ({exc}); using original track.")
        return None


def _apply_corrections(result: dict, corrections: list) -> dict:
    """Whole-word find/replace over the transcript (case-insensitive).

    Whisper mishears the same slang/name the same way every run; a per-project
    corrections list fixes those systematically. Applied AFTER decode (not folded
    into the decode cache) so editing corrections takes effect with no re-decode.
    Single-word sources are fixed in both segment text and the word stream;
    multi-word source phrases are fixed in segment text only (the word stream is
    one token per entry)."""
    compiled = []
    for pair in corrections or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        wrong, right = str(pair[0]).strip(), str(pair[1])
        if not wrong:
            continue
        compiled.append(
            (re.compile(r"\b" + re.escape(wrong) + r"\b", re.IGNORECASE), right))
    if not compiled:
        return result

    def fix(text: str) -> str:
        for pat, rep in compiled:
            text = pat.sub(rep, text)
        return text

    for seg in result.get("segments", []):
        seg["text"] = fix(seg["text"])
    for w in result.get("words", []):
        lead = " " if w["word"][:1] == " " else ""
        w["word"] = lead + fix(w["word"].strip())
    return result


def _report_coverage(result: dict, dur: float, gap_min: float = 8.0) -> None:
    """Print what fraction of the source actually got transcribed and where the
    holes are. Read-only — never changes the transcript. A long GAP is where
    Whisper/VAD dropped (or the source genuinely had) no words; inspect those to
    decide whether coverage_mode is worth turning on for this project."""
    words = result.get("words", [])
    if not words:
        print("[transcribe] coverage: 0 words transcribed")
        return
    total = dur or result.get("duration") or words[-1]["end"]
    speech_start, last, covered, gaps = words[0]["start"], words[0]["end"], 0.0, []
    for w in words[1:]:
        if w["start"] - last > gap_min:
            covered += last - speech_start
            gaps.append((last, w["start"]))
            speech_start = w["start"]
        last = max(last, w["end"])
    covered += last - speech_start
    pct = 100.0 * covered / total if total else 0.0
    print(f"[transcribe] coverage {pct:.1f}% (source {total:.0f}s, "
          f"speech {covered:.0f}s, {len(words)} words)")
    head, tail = words[0]["start"], (total - last if total else 0.0)
    if head > gap_min:
        print(f"[transcribe]   GAP 0.0-{head:.1f}s ({head:.1f}s, before first word)")
    for a, b in gaps:
        print(f"[transcribe]   GAP {a:.1f}-{b:.1f}s ({b - a:.1f}s, no words)")
    if tail > gap_min:
        print(f"[transcribe]   GAP {last:.1f}-{total:.1f}s ({tail:.1f}s, after last word)")


def transcribe(video: Path, cfg: Config) -> dict:
    """Return a transcript dict:
    {"language": str, "words": [{"start","end","word"}...],
     "segments": [{"start","end","text"}...]}
    Cached to work/transcripts/<hash>.json.
    """
    tconf = cfg.transcribe
    model_name = tconf.get("model", "large-v3")
    lang = tconf.get("language", "auto")
    initial_prompt = str(tconf.get("initial_prompt", "") or "").strip()
    hotwords = str(tconf.get("hotwords", "") or "").strip()
    vad_params = dict(tconf.get("vad", {}) or {})
    corrections = tconf.get("corrections", []) or []

    # High-coverage mode: the two gates that silently DROP whole spans of speech
    # are the Silero VAD threshold (borderline/quiet talk trimmed before Whisper
    # sees it) and no_speech_threshold (segments classified as silence). Loosen
    # both so laughter-covered / cross-talk / quiet passages survive. Opt-in per
    # project (new-project template turns it on); existing configs are untouched.
    # An explicit [transcribe.vad] threshold still wins if the project sets one.
    coverage = bool(tconf.get("coverage_mode", False))
    no_speech = float(tconf.get("no_speech_threshold", 0.6))
    if coverage:
        vad_params.setdefault("threshold", 0.2)
        no_speech = max(no_speech, 0.85)

    cache_dir = cfg.work_dir / "transcripts"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Any setting that changes the output must invalidate the cache, so fold them
    # all into a short signature appended to the readable model_lang key.
    dur = _probe_duration(video, cfg)
    long_min = float(tconf.get("long_form_minutes", 20))
    is_long = dur > long_min * 60 or (dur == 0.0 and cfg.ffprobe == "")
    beam = int(tconf.get("long_form_beam_size", 1) if is_long
               else tconf.get("beam_size", 5))
    condition = False if is_long else bool(
        tconf.get("condition_on_previous_text", True))

    # corrections are a post-decode string pass, deliberately NOT in the hash:
    # editing corrections.txt must not force an hour-long re-decode.
    sig_src = json.dumps({
        "m": model_name, "l": lang, "beam": beam, "cond": condition,
        "prompt": initial_prompt, "hot": hotwords, "vad": vad_params,
        "cov": coverage,
        "guards": [no_speech,
                   tconf.get("compression_ratio_threshold", 2.4),
                   tconf.get("log_prob_threshold", -1.0)],
        "audio": tconf.get("audio", {}),
    }, sort_keys=True, ensure_ascii=False)
    param_hash = hashlib.sha1(sig_src.encode("utf-8")).hexdigest()[:8]
    key = f"{file_hash(video)}_{model_name}_{lang}_{param_hash}"
    cache_file = cache_dir / f"{key}.json"
    gap_min = float(tconf.get("coverage_gap_seconds", 8.0))
    if cache_file.exists():
        print(f"[transcribe] cache hit {cache_file.name}")
        cached = _apply_corrections(
            json.loads(cache_file.read_text(encoding="utf-8")), corrections)
        _report_coverage(cached, dur, gap_min)
        return cached

    _enable_cuda_dlls()
    device, compute_type = _pick_device(
        tconf.get("device", "auto"), tconf.get("compute_type", ""))
    if device == "cuda":
        _free_ollama_vram(cfg)
    if dur:
        print(f"[transcribe] duration {dur / 60:.1f} min -> "
              f"{'long-form' if is_long else 'standard'} settings "
              f"(beam={beam}, condition={condition})")
    print(f"[transcribe] model={model_name} device={device} ({compute_type})")

    audio_cache = cfg.work_dir / "audio"
    audio_cache.mkdir(parents=True, exist_ok=True)
    audio_in = _clean_audio(video, cfg, audio_cache, key) or video

    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
    except Exception as exc:
        if device == "cuda":
            print(f"[transcribe] CUDA init failed ({exc}); falling back to CPU.")
            device, compute_type = "cpu", "int8"
            model = WhisperModel(model_name, device=device, compute_type=compute_type)
        else:
            raise

    # beam_size + condition_on_previous_text are chosen above by length:
    # long-form podcasts get greedy/no-context to avoid the repetition loop that
    # makes word_timestamps crawl; shorter clips get beam search for accuracy.
    kwargs = {
        "word_timestamps": True,
        "vad_filter": True,
        "beam_size": beam,
        "condition_on_previous_text": condition,
        "no_speech_threshold": no_speech,
        "compression_ratio_threshold": float(
            tconf.get("compression_ratio_threshold", 2.4)),
        "log_prob_threshold": float(tconf.get("log_prob_threshold", -1.0)),
    }
    if lang and lang != "auto":
        kwargs["language"] = lang
    # initial_prompt only conditions the FIRST decode window and, on long-form,
    # actively disrupts the greedy/no-context decode (measured: it dropped Yono
    # whole-file coverage 99.7%->90.6% with big wordless gaps). So use it only on
    # short sources; long-form relies on hotwords instead, which bias the WHOLE
    # file and survive condition_on_previous_text=False.
    if initial_prompt and not is_long:
        kwargs["initial_prompt"] = initial_prompt
    if hotwords:
        kwargs["hotwords"] = hotwords
    if vad_params:
        kwargs["vad_parameters"] = dict(vad_params)

    segments, info = model.transcribe(str(audio_in), **kwargs)

    words, segs = [], []
    for seg in segments:
        segs.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
        for w in (seg.words or []):
            words.append({"start": w.start, "end": w.end, "word": w.word})

    result = {
        "language": info.language,
        "duration": info.duration,
        "words": words,
        "segments": segs,
    }
    # Cache the raw whisper output; corrections are applied on read (here and on
    # cache hits) so they stay editable without invalidating the decode cache.
    cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(f"[transcribe] {len(words)} words, {len(segs)} segments, lang={info.language}")
    out = _apply_corrections(result, corrections)
    _report_coverage(out, dur, gap_min)
    return out


def transcribe_span(video: Path, cfg: Config, start: float, end: float) -> list[dict]:
    """Re-transcribe a single [start, end] window PROMPT-FREE and HOTWORD-FREE.

    The whole-video long-form pass biases decoding with the project Words.txt
    (applied as hotwords), which on quiet/ambiguous audio echoes that vocab back
    as fake captions ("Sun Go Kong Sun Go Kong"), and its single long decode
    drifts / hallucinates on silence and code-switched speech. Re-decoding just
    the clip with no prompt, no hotwords, no cross-segment context and VAD on
    removes the echo and the silence hallucinations. Returns words in SOURCE
    time; corrections.txt is still applied so systematic name fixes survive.
    """
    import tempfile
    tconf = cfg.raw.get("transcribe", {})
    corrections = tconf.get("corrections", []) or []
    model_name = str(tconf.get("model", "large-v3"))
    lang = str(tconf.get("language", "") or "").strip().lower()
    lang = None if lang in ("", "auto") else lang

    _enable_cuda_dlls()
    device, compute_type = _pick_device(
        tconf.get("device", "auto"), tconf.get("compute_type", ""))
    from faster_whisper import WhisperModel
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
    except Exception as exc:
        if device == "cuda":
            print(f"[retranscribe] CUDA init failed ({exc}); CPU fallback.")
            device, compute_type = "cpu", "int8"
            model = WhisperModel(model_name, device=device, compute_type=compute_type)
        else:
            raise

    dur = max(0.05, round(end - start, 3))
    wav = Path(tempfile.gettempdir()) / f"clipcraft_rt_{int(start*1000)}_{int(end*1000)}.wav"
    # Denoise the span before decoding. A music / room-tone bed under the speech
    # makes whisper mark whole spans as no-speech and silently drop them (the
    # opening seconds vanish, mid-clip lines go missing). highpass + afftdn strips
    # the bed; loudnorm evens levels so quiet talkers still register. Verified on
    # the Pojok Jenaka clip_01 raw cut: 80 dropped-span words -> 133 with coverage
    # from 0.0s. A SECOND afftdn pass over-denoises into garble, so keep it single.
    aconf = cfg.transcribe.get("audio", {})
    filters = [f"highpass=f={int(aconf.get('highpass', 120))}",
               "afftdn=nf=-25",
               "loudnorm=I=-16:TP=-1.5:LRA=11"]
    subprocess.run([cfg.ffmpeg or "ffmpeg", "-y", "-ss", f"{start:.3f}",
                    "-t", f"{dur:.3f}", "-i", str(video), "-af", ",".join(filters),
                    "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    kwargs = {
        "word_timestamps": True, "beam_size": 5,
        "condition_on_previous_text": False, "no_speech_threshold": 0.4,
        "compression_ratio_threshold": 2.6, "log_prob_threshold": -1.2,
        # Let whisper retry a hard window at higher temperature instead of
        # emitting nothing (default 0.0 gives up and drops the span).
        "temperature": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        # VAD off: on a denoised span its speech/silence call is unreliable and
        # was itself dropping the opening. no_speech_threshold now gates instead.
        "vad_filter": False,
        # Kill the "ini ini ini..." token loop whisper falls into over music /
        # laughter / crosstalk: banning any repeated 3-gram forces it to decode
        # the real words underneath instead of stalling on one token.
        "no_repeat_ngram_size": 3,
    }
    if lang:
        kwargs["language"] = lang
    # No initial_prompt, no hotwords -> the Words.txt echo cannot be injected.
    try:
        segments, _info = model.transcribe(str(wav), **kwargs)
        words = []
        for seg in segments:
            for w in (seg.words or []):
                words.append({"start": round(start + w.start, 3),
                              "end": round(start + w.end, 3), "word": w.word})
    finally:
        try:
            wav.unlink()
        except OSError:
            pass
    print(f"[retranscribe] {start:.1f}-{end:.1f}s -> {len(words)} words "
          f"(prompt-free, hotword-free)")
    return _apply_corrections({"words": words}, corrections).get("words", words)
