"""Boundary snapping. The LLM's timestamps are a rough guess and will be 1-3s
off in ways that feel wrong. Snap to word edges, grow short hooks to a postable
length, add padding, avoid mid-sentence cuts. This single step fixes most
'the clip feels wrong' complaints."""

from __future__ import annotations

from .config import Config

# Word tokens that make a natural stopping point.
_SENTENCE_END = (".", "!", "?", "…")


def _nearest_word_start(words: list[dict], t: float) -> float:
    if not words:
        return t
    return min(words, key=lambda w: abs(w["start"] - t))["start"]


def _sentence_end_near(words: list[dict], target: float, window: float = 6.0) -> float:
    """End of a sentence-final word closest to `target`, searched within
    +/- window seconds. Falls back to the end of the word nearest target."""
    if not words:
        return target
    enders = [w for w in words
              if w["word"].strip().endswith(_SENTENCE_END)
              and abs(w["end"] - target) <= window]
    if enders:
        return min(enders, key=lambda w: abs(w["end"] - target))["end"]
    return min(words, key=lambda w: abs(w["end"] - target))["end"]


def _next_sentence_end(words: list[dict], after: float) -> float | None:
    """End of the first sentence-final word that ends after `after`."""
    for w in words:
        if w["end"] > after + 0.05 and w["word"].strip().endswith(_SENTENCE_END):
            return w["end"]
    return None


def snap(candidate: dict, transcript: dict, cfg: Config) -> dict:
    words = transcript.get("words", [])
    clip = cfg.clip
    lead = clip.get("lead_in", 0.25)
    tail = clip.get("tail", 0.35)
    min_s = clip.get("min_seconds", 10)
    # House rule (ALWAYS ENFORCE): content span never exceeds 90s, even if a
    # project config sets a higher max_seconds.
    max_s = min(float(clip.get("max_seconds", 90)), 90.0)
    duration = transcript.get("duration") or (words[-1]["end"] if words else 0)

    start = _nearest_word_start(words, candidate["start"])
    # Natural end of the thought: the sentence boundary nearest the model's end.
    end = _sentence_end_near(words, candidate["end"])

    # Only if unpostably short, grow by WHOLE following sentences until we clear
    # the floor — never pad mid-sentence, never inflate to a fixed target.
    while end - start < min_s:
        nxt = _next_sentence_end(words, end)
        if nxt is None or nxt - start > max_s:
            break
        end = nxt
    # Fallback for weak punctuation (common in Indonesian ASR): if no sentence
    # boundary was available to reach the floor, extend to the nearest word edge
    # around start+min_s so we never ship a sub-floor fragment.
    if end - start < min_s:
        floor_end = _sentence_end_near(words, start + min_s)
        end = min(max(end, floor_end), start + max_s)
    # Over the ceiling: trim back to the last sentence end that fits.
    if end - start > max_s:
        end = _sentence_end_near(words, start + max_s)

    start = max(0.0, start - lead)
    end = min(duration, end + tail)
    if end <= start:
        end = min(duration, start + min_s)
    # Hard ceiling: lead/tail padding must not push the final span past max_s.
    if end - start > max_s:
        end = start + max_s

    snapped = dict(candidate)
    snapped["start"] = round(start, 2)
    snapped["end"] = round(end, 2)
    snapped["duration"] = round(end - start, 2)
    return snapped


def snap_all(candidates: list[dict], transcript: dict, cfg: Config) -> list[dict]:
    return [snap(c, transcript, cfg) for c in candidates]
