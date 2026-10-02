"""LLM candidate selection via local Ollama.

The judgment layer. Over-propose: recall beats precision here — the human is
the filter and they are fast at it.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from .config import Config

# Han, kana, bopomofo, hangul, CJK punctuation and full-width forms. Qwen
# occasionally emits these even on an Indonesian transcript; captions and titles
# must never ship them, so we strip deterministically rather than hope a prompt
# holds.
_CJK_RE = re.compile(
    r"[ᄀ-ᇿ⺀-⻿　-〿぀-ヿ㄰-㆏"
    r"㐀-䶿一-鿿ꥠ-꥿가-퟿豈-﫿＀-￯]"
)

_LANG_NAMES = {"id": "Indonesian", "en": "English", "ms": "Malay"}


def has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s or ""))


def strip_cjk(s: str) -> str:
    """Drop CJK characters and tidy the whitespace they leave behind."""
    return re.sub(r"\s{2,}", " ", _CJK_RE.sub("", s or "")).strip()


class OllamaUnreachable(RuntimeError):
    """The local Ollama server isn't answering — it's almost always not running."""


def _ollama_base(cfg: Config) -> str:
    return cfg.select.get("ollama_url", "http://localhost:11434").rstrip("/")


def _check_ollama(cfg: Config) -> None:
    """Fail fast with a clear message if Ollama isn't up, instead of grinding
    through every chunk with retries and a misleading timeout hint."""
    url = _ollama_base(cfg) + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5):
            return
    except (urllib.error.URLError, OSError) as exc:
        raise OllamaUnreachable(
            f"Cannot reach Ollama at {_ollama_base(cfg)} ({exc}). Start it first:\n"
            "  ollama serve      (or launch the Ollama app)\n"
            "then re-run. The transcript is cached, so this resumes fast."
        ) from exc

SYSTEM = (
    "You are a senior short-form video editor. You find self-contained moments "
    "in a transcript that would make strong vertical clips (TikTok/Reels/Shorts). "
    "A good clip delivers ONE complete payload: the setup, the tension or question "
    "it raises, AND the payoff (the punchline, reveal, or answer). A viewer who "
    "saw nothing before it must get the full value and feel the moment landed — "
    "never end on a setup whose payoff comes later. Carry the point to its natural "
    "conclusion, then stop. Length follows the content: a punchy one-line reveal "
    "can be 15-25s; a story that needs its build-up can run to the ceiling. Never "
    "pad a thin moment and never truncate a complete one to hit a number. Avoid "
    "moments that depend on something said much earlier. Prefer complete sentences."
)


def _transcript_lines(segments: list[dict]) -> list[str]:
    lines = []
    for seg in segments:
        lines.append(f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}")
    return lines


def _chunks(lines: list[str], max_chars: int = 9000) -> list[list[str]]:
    out, cur, size = [], [], 0
    for ln in lines:
        if size + len(ln) > max_chars and cur:
            out.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        out.append(cur)
    return out


def _call_ollama(cfg: Config, prompt: str) -> dict:
    conf = cfg.select
    url = conf.get("ollama_url", "http://localhost:11434").rstrip("/") + "/api/generate"
    payload = {
        "model": conf.get("model", "llama3.1:8b"),
        "system": SYSTEM,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "keep_alive": "10m",  # keep the model resident between chunk calls
        "options": {
            "temperature": 0.4,
            "num_ctx": int(conf.get("num_ctx", 8192)),
            "num_predict": int(conf.get("max_output_tokens", 2048)),
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    timeout = int(conf.get("request_timeout", 600))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    try:
        return json.loads(body["response"])
    except (KeyError, json.JSONDecodeError):
        return {"candidates": []}


def _build_prompt(lines: list[str], want: int, clip: dict, brief: str = "",
                  lang_name: str = "Indonesian", people: list | None = None) -> str:
    lo = clip.get("min_seconds", 10)
    hi = clip.get("max_seconds", 60)
    transcript = "\n".join(lines)
    lang_block = (
        f"LANGUAGE: Write every text field (title, description, hook) in "
        f"{lang_name}. Never output Chinese, Japanese, Korean, or any other "
        "CJK characters under any circumstance.\n\n"
    )
    people_block = ""
    if people:
        roster = ", ".join(str(p) for p in people)
        people_block = (
            f"PEOPLE IN THIS VIDEO (the ONLY participants): {roster}.\n"
            "When a title/description/hook refers to someone in the video, use "
            "ONLY a name from this list. NEVER invent a name or promote a name "
            "that is not on this list — a name that appears once or twice and "
            "matches none of these is almost always a mis-transcription, so do "
            "NOT build a clip or title around it. (Public figures being DISCUSSED "
            "— not participants — may be named as themselves, e.g. a celebrity the "
            "participants talk about.)\n\n"
        )
    brief_block = ""
    if brief.strip():
        brief_block = (
            "CREATOR BRIEF — follow these do/don't rules strictly. Do NOT propose "
            "any clip that violates a DON'T, even if the moment is strong:\n"
            f"{brief.strip()}\n\n"
        )
    return (
        lang_block
        + people_block
        + brief_block
        + f"Transcript. Each line is [start-end in seconds] text:\n\n{transcript}\n\n"
        f"Pick up to {want} clip candidates. Each clip must cover ONE COMPLETE "
        "topic: set `start` on the first words that introduce the topic and `end` "
        "where that topic is fully resolved — the natural conclusion of the point. "
        "Do NOT end the clip while the same topic is still being explained; carry "
        "it all the way to its payoff even if that makes the clip longer. "
        f"Let the content decide the length: use however long the whole topic needs "
        f"— at least {lo}s and at most {hi}s. Do NOT cut a topic off early, and do "
        "NOT pad a thin moment just to hit a number. Use the second values from "
        "the lines to set start and end.\n"
        f"IMPORTANT: {lo}s is a floor, NOT a target. Do NOT default to short clips. "
        "Extend `end` to the natural conclusion of the point even when that runs to "
        "40, 60, or 90s. Only produce a clip near the floor when the moment is "
        "genuinely complete and self-contained in that span. Aim for a MIX of "
        "lengths across your candidates, matched to each moment.\n"
        "WHAT MAKES A MOMENT WORTH CLIPPING — judge it by the STRUCTURE of what is "
        "said, NEVER by whether the room laughed:\n"
        "- FUNNY: a real comedic beat in the words — a setup then a punchline, an "
        "unexpected turn, irony, a callback, a roast, or an absurd claim delivered "
        "straight. No laugh track is required; a joke that lands in the transcript "
        "counts, and a laugh with no joke does not.\n"
        "- INSIGHTFUL: a concrete claim, number, lesson, confession, strong opinion, "
        "or a 'wait, really?' reveal that teaches or genuinely surprises.\n"
        "SELF-CONTAINED (VIEWER POV) — assume the viewer saw NOTHING before this "
        "clip. Inside [start, end] they MUST be able to tell WHO is being discussed "
        "(a name or an unmistakable subject appears in the clip — not a bare "
        "'dia/ia/itu/ini/mereka' that points at something said earlier), WHAT the "
        "point is, and feel the payoff land. If the moment depends on setup from "
        "before it, either move `start` EARLIER to include that setup, or do not "
        "pick the moment. A clip a stranger can't follow is worthless.\n"
        "Return ONLY JSON of this exact shape:\n"
        '{"candidates":[{"start":<seconds float>,"end":<seconds float>,'
        '"hook_start":<seconds float>,"hook_end":<seconds float>,'
        '"title":"<curiosity-gap headline the viewer sees first, SAME language as '
        'the transcript, 6-14 words, no quotes. It MUST stand alone with full '
        'context so it makes sense to someone who saw none of the video: name the '
        'specific PERSON or SUBJECT using proper names from the transcript (never '
        'vague pronouns like he/she/this guy), state the concrete CLAIM, NUMBER or '
        'ACTION, and name the SOURCE or relationship when it adds stakes. Model it '
        'on PUTRI TANJUNG SPILL GAJINYA PER BULAN DARI CHAIRUL TANJUNG — it names '
        'who, the specific what, and from whom. Reject vague teasers such as This '
        'Changed Everything that hide who and what>",'
        '"description":"<a 1-2 sentence video description for this specific clip, '
        'SAME language as the transcript, engaging, no hashtags>",'
        '"hook":"<a scroll-stopping teaser, SAME language as the transcript, 3-7 '
        'words, curiosity or stakes, NOT a verbatim quote, no ending punctuation>",'
        '"why":"<one sentence: why it clips>",'
        '"score":<0-10 float>,"risks":["..."]}]}\n'
        "The title is the big headline a viewer sees first — it is burned onto the "
        "clip as a title card, so it must be self-explanatory with names and "
        "specifics, not a vague tease. `hook_start` and "
        "`hook_end` are REQUIRED for every candidate: mark the SINGLE line inside "
        "[start, end] that opens the biggest CURIOSITY GAP or emotional spike — a "
        "shocking claim, a provocative question, a punchline setup, or the most "
        "'wait, what?' sentence — NOT necessarily the first line. Set hook_start on "
        "its first word and hook_end on its last word. It gets copied to the very "
        "front as a cold open, so pick the line most likely to stop a scroll and "
        "make the viewer need the rest. Both must lie within start and end. Order by "
        "score descending. If a moment needs earlier context, list that in risks "
        "rather than omitting it."
    )


def _sanitize(cands: list[dict], duration: float, clip: dict) -> list[dict]:
    # House rule (ALWAYS ENFORCE): 90s hard ceiling, even if config sets higher.
    hi = min(float(clip.get("max_seconds", 90)), 90.0)
    out = []
    for c in cands:
        try:
            start = max(0.0, float(c["start"]))
            end = min(duration, float(c["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start or (end - start) < 1.0:
            continue
        # Short single-sentence hooks are fine here; snap() grows them to a
        # postable length. Only clamp absurdly long spans.
        if (end - start) > hi * 1.5:
            end = start + hi
        entry = {
            "start": round(start, 2),
            "end": round(end, 2),
            "title": strip_cjk(str(c.get("title", ""))),
            "description": strip_cjk(str(c.get("description", ""))),
            "hook": strip_cjk(str(c.get("hook", ""))),
            "why": strip_cjk(str(c.get("why", ""))),
            "score": float(c.get("score", 0) or 0),
            "risks": [s for s in (strip_cjk(str(r)) for r in (c.get("risks") or [])) if s],
        }
        # Cold-open hook window, if the model marked one inside the clip.
        try:
            hs, he = float(c["hook_start"]), float(c["hook_end"])
            if start <= hs < he <= end:
                entry["hook_start"] = round(hs, 2)
                entry["hook_end"] = round(he, 2)
        except (KeyError, TypeError, ValueError):
            pass
        out.append(entry)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


# Indonesian openers that point at something said earlier — a clip that STARTS
# on one of these usually drops the viewer mid-thought with no subject. Whole-word
# match at the very start of the clip's first line.
_DANGLING_OPENERS = {
    "dia", "ia", "mereka", "nya", "itu", "ini", "beliau", "tersebut",
    "jadi", "terus", "trus", "makanya", "soalnya", "pokoknya", "nah",
    "intinya", "gara-gara", "akhirnya", "terusnya", "abis", "habis",
}


def _opens_dangling(text: str) -> bool:
    first = re.sub(r"^[^\w]+", "", (text or "").lower()).split()
    return bool(first) and first[0] in _DANGLING_OPENERS


# The model flags context-dependence in its own `risks` field ("needs earlier
# context about X", "requires context from previous discussion"). We act on that
# signal instead of guessing from the transcript alone.
_CONTEXT_RISK_RE = re.compile(
    r"\b(earlier|previous|prior|before this|preceding|context from|needs? context|"
    r"requires? context|sebelumnya|konteks)\b", re.IGNORECASE)
_TOPIC_QUOTED_RE = re.compile(r"['\"]([^'\"]{2,40})['\"]")
_TOPIC_STOP = {
    "the", "this", "that", "earlier", "context", "about", "from", "previous",
    "prior", "clip", "video", "discussion", "yang", "tentang", "dari", "ini",
    "itu", "dan", "needs", "requires", "part", "parts", "unfamiliar", "those",
}


def _risk_flags_context(risks) -> bool:
    return any(_CONTEXT_RISK_RE.search(str(r or "")) for r in (risks or []))


def _topic_terms(text: str) -> list[str]:
    """Proper-noun / quoted topic keys the clip depends on — pulled from the risk
    text and title so we can search the transcript for where they were set up."""
    terms = set(m.strip() for m in _TOPIC_QUOTED_RE.findall(text or ""))
    for tok in re.findall(r"\b([A-Z][A-Za-z]{2,})\b", text or ""):
        terms.add(tok)
    return [t for t in terms if t and t.lower() not in _TOPIC_STOP]


def apply_context_guard(candidates: list[dict], transcript: dict,
                        max_lookback: float = 25.0,
                        demote: float = 0.85) -> list[dict]:
    """Enforce the "viewer gets all context in one clip" rule using the model's own
    context signal plus the transcript. For each clip:

    1. If it opens on a dangling reference (bare pronoun / discourse marker), pull
       `start` back to the sentence that names the subject.
    2. If the model flagged it as needing earlier context, find where the topic it
       depends on was introduced. If that introduction is within ``max_lookback``
       seconds, pull `start` back to include it (now self-contained). If it is out
       of reach (topic set up minutes earlier), the clip CANNOT be made
       self-contained by extension — demote its score by ``demote`` so genuinely
       self-contained clips win the top render slots, and flag it for review.

    Runs before snapping (which word-aligns the new start); any overlaps it creates
    are cleaned by resolve_overlaps afterwards. Conservative — never drops a clip,
    only re-times or re-ranks, so the human still sees everything."""
    segs = transcript.get("segments", [])
    if not segs:
        return candidates
    import bisect
    starts = [s["start"] for s in segs]
    for c in candidates:
        cs = float(c.get("start", 0.0))
        i = bisect.bisect_right(starts, cs + 0.25) - 1
        if i < 0:
            continue

        # 1) Dangling opener -> back off to the subject sentence.
        if _opens_dangling(segs[i].get("text", "")):
            floor = cs - max_lookback
            j = i
            for _ in range(3):
                if j - 1 < 0 or segs[j - 1]["start"] < floor:
                    break
                j -= 1
                if not _opens_dangling(segs[j].get("text", "")):
                    break
            if j < i:
                c["start"] = round(segs[j]["start"], 2)
                c["_context_expanded"] = round(cs - float(c["start"]), 2)

        # 2) Model flagged earlier-context dependency -> repair or demote.
        if _risk_flags_context(c.get("risks")):
            cur = float(c["start"])
            ci = bisect.bisect_right(starts, cur + 0.25) - 1
            floor = cur - max_lookback
            terms = _topic_terms(
                " ".join(map(str, c.get("risks") or [])) + " " + str(c.get("title", "")))
            intro = None
            for k in range(ci, -1, -1):                 # earliest topic mention in window
                if segs[k]["start"] < floor:
                    break
                low = segs[k].get("text", "").lower()
                if any(t.lower() in low for t in terms):
                    intro = segs[k]["start"]
            if intro is not None and intro < cur:
                c["_context_expanded"] = round(cs - round(intro, 2), 2)
                c["start"] = round(intro, 2)
            else:
                # Couldn't pull the setup in. Only DEMOTE when the clip STILL
                # literally opens mid-thought (a bare pronoun / discourse marker) —
                # qwen slaps "needs earlier context" on almost every clip, so its
                # risk text alone is far too noisy to penalise (it was tanking
                # good self-contained clips). Require the structural dangling-open
                # signal, and nudge gently rather than cliff.
                ci2 = bisect.bisect_right(starts, float(c["start"]) + 0.25) - 1
                if ci2 >= 0 and _opens_dangling(segs[ci2].get("text", "")):
                    c["score"] = round(float(c.get("score", 0) or 0) * demote, 3)
                    c["context_unresolved"] = True
                else:
                    c["context_note"] = "model flagged possible earlier context"
    return candidates


def resolve_overlaps(candidates: list[dict], min_seconds: float = 10.0,
                     trim_tol: float = 1.5) -> list[dict]:
    """Enforce the house rule that no two shipped clips share source time.

    Greedy by score: keep the strongest clip, then for each weaker candidate
    check it against everything already kept. A clip that only *kisses* a kept
    one (a small padding overlap up to ``trim_tol`` seconds) is trimmed to abut
    it; a clip that genuinely re-cuts the same moment (a larger overlap) is
    dropped. Trimming only ever shrinks a span, so it can never introduce a new
    overlap with an already-checked clip — the returned set is guaranteed
    pairwise non-overlapping. A clip trimmed below ``min_seconds`` is dropped
    rather than shipped as a fragment; a clip already short on its own (never
    trimmed) is left alone.

    See CLAUDE.md "Clip selection: no overlap" — this is the code enforcement of
    that rule; boundary snapping (snap.py) can push adjacent clips into each
    other via lead/tail padding, so run this after snapping."""
    kept: list[dict] = []
    for c in sorted(candidates, key=lambda x: x.get("score", 0), reverse=True):
        s, e = float(c["start"]), float(c["end"])
        drop = False
        trimmed = False
        for k in kept:
            ks, ke = k["start"], k["end"]
            ov = min(e, ke) - max(s, ks)
            if ov <= 0:
                continue
            if ov > trim_tol:            # same moment -> keep the stronger clip
                drop = True
                break
            trimmed = True               # padding kiss -> trim off the kept clip
            if s < ks:
                e = ks
            else:
                s = ke
        if drop:
            continue
        if trimmed and (e - s) < min_seconds:
            continue
        entry = dict(c)
        entry["start"] = round(s, 2)
        entry["end"] = round(e, 2)
        entry["duration"] = round(e - s, 2)
        kept.append(entry)
    kept.sort(key=lambda x: x.get("score", 0), reverse=True)
    return kept


def select(transcript: dict, cfg: Config, n: int | None = None,
           brief: str = "") -> list[dict]:
    want = n or cfg.select.get("candidates", 10)
    segments = transcript.get("segments", [])
    duration = transcript.get("duration") or (segments[-1]["end"] if segments else 0)
    if not segments:
        return []

    _check_ollama(cfg)  # fail fast + clearly if the server is down
    lang_code = (transcript.get("language") or "id").lower()
    lang_name = _LANG_NAMES.get(lang_code, "Indonesian")
    lines = _transcript_lines(segments)
    chunks = _chunks(lines, int(cfg.select.get("chunk_chars", 9000)))
    # Cap candidates requested per Ollama call: asking an 8B model for dozens of
    # JSON items in one shot is very slow and can time out. We still collect
    # across all chunks, so total candidates ~= chunks * per_chunk.
    cap = int(cfg.select.get("max_per_chunk", 12) or 12)
    per_chunk = min(cap, max(3, -(-want // len(chunks)) + 1))
    people = cfg.select.get("people") or []
    if brief.strip():
        print("[select] applying creator brief")
    if people:
        print(f"[select] roster: {', '.join(map(str, people))}")

    collected: list[dict] = []
    failed = 0
    for i, chunk in enumerate(chunks):
        print(f"[select] scoring chunk {i + 1}/{len(chunks)} "
              f"({len(chunk)} segments, up to {per_chunk} candidates)")
        result = None
        for attempt in range(2):  # one retry before giving up on a chunk
            try:
                result = _call_ollama(
                    cfg, _build_prompt(chunk, per_chunk, cfg.clip, brief,
                                       lang_name, people))
                break
            except Exception as exc:
                if attempt == 0:
                    print(f"[select]   chunk {i + 1} {type(exc).__name__}, retrying...")
                else:
                    print(f"[select]   chunk {i + 1} FAILED twice ({exc}); skipping. "
                          "This section of the video will be under-covered — raise "
                          "[select] request_timeout or lower max_per_chunk.")
        if result is None:
            failed += 1
            continue
        collected.extend(result.get("candidates", []))
    if failed:
        print(f"[select] WARNING: {failed}/{len(chunks)} chunks failed — coverage "
              "is uneven. See the message above to fix.")

    ranked = _sanitize(collected, duration, cfg.clip)
    return ranked[:want]
