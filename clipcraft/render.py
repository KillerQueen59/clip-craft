"""Cut, reframe to 9:16, and burn captions with ffmpeg.

Reframe is a stable center-crop (cover then crop). Stability beats accuracy:
a rock-steady slightly-off crop looks professional; a jittery centered one
looks broken. Face-tracking auto-reframe is a later, opt-in layer.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import Config


def _fmt_ass_time(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs == 100:
        cs = 0
        s += 1
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _group_words(words: list[dict], start: float, end: float,
                 max_words: int = 3, max_chars: int = 16) -> list[dict]:
    """Words within [start,end], grouped into short caption lines timed relative
    to `start`. Each event also carries per-word timing (`words`) so the caption
    can be rendered karaoke-style with the active word highlighted."""
    inwin = [w for w in words if w["end"] > start and w["start"] < end]
    lines, cur = [], []
    cur_chars = 0
    for w in inwin:
        token = w["word"].strip()
        if not token:
            continue
        if cur and (len(cur) >= max_words or cur_chars + len(token) > max_chars):
            lines.append(cur)
            cur, cur_chars = [], 0
        cur.append(w)
        cur_chars += len(token) + 1
    if cur:
        lines.append(cur)

    events = []
    for grp in lines:
        text = " ".join(w["word"].strip() for w in grp).strip()
        text = re.sub(r"\s+([,.!?…])", r"\1", text)
        s = max(0.0, grp[0]["start"] - start)
        e = min(end - start, grp[-1]["end"] - start + 0.15)
        if e <= s:
            e = s + 0.4
        wtimes = [{"s": max(0.0, w["start"] - start),
                   "e": max(0.0, w["end"] - start),
                   "w": w["word"].strip()} for w in grp]
        events.append({"start": s, "end": e, "text": text, "words": wtimes})
    return events


def _ass_color(hex_str: str, opacity: float = 1.0) -> str:
    """#RRGGBB -> ASS &HAABBGGRR (alpha 00=opaque, FF=transparent)."""
    h = hex_str.strip().lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    a = int((1.0 - max(0.0, min(1.0, opacity))) * 255)
    return f"&H{a:02X}{b:02X}{g:02X}{r:02X}"


def _ass_inline(hex_str: str) -> str:
    """#RRGGBB -> ASS inline \\c override value &HBBGGRR& (no alpha)."""
    h = hex_str.strip().lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _esc(text: str) -> str:
    return text.replace("\n", " ").replace("{", "(").replace("}", ")")


def _wrap(text: str, max_chars: int) -> str:
    """Greedy word-wrap into ASS lines joined by \\N."""
    words, lines, cur = text.split(), [], ""
    for wd in words:
        if cur and len(cur) + 1 + len(wd) > max_chars:
            lines.append(cur)
            cur = wd
        else:
            cur = f"{cur} {wd}".strip()
    if cur:
        lines.append(cur)
    return "\\N".join(lines)


def _caption_style(cap: dict, scale: float, h: int) -> str:
    size = int(cap.get("font_size", 64) * scale)
    margin_v = int(cap.get("bottom_margin", 0.18) * h)
    bold = -1 if cap.get("bold", True) else 0
    primary = _ass_color(cap.get("text_color", "#FFFFFF"))
    if cap.get("style", "outline") == "box":
        border, oc = 3, _ass_color(cap.get("box_color", "#000000"),
                                   cap.get("box_opacity", 0.6))
        outline, shadow = max(6, int(cap.get("outline_width", 4) * scale)), 0
    else:
        border, oc = 1, _ass_color(cap.get("outline_color", "#000000"))
        outline = int(cap.get("outline_width", 4) * scale)
        shadow = int(cap.get("shadow", 2) * scale)
    return (f"Style: Cap,{cap.get('font', 'Arial Black')},{size},{primary},{oc},"
            f"&H00000000,{bold},0,{border},{outline},{shadow},2,60,60,{margin_v},1")


def _write_ass(events: list[dict], cfg: Config, path: Path, w: int, h: int,
               title: str = "", clip_seconds: float = 0.0, hook: str = "",
               title_seconds: float | None = None, outro_text: str = "") -> None:
    cap = cfg.captions
    scale = w / 1080
    styles = [_caption_style(cap, scale, h)]
    dialogues = []
    full_end = _fmt_ass_time(max(clip_seconds, 0.1))

    # --- hook card (first N seconds) ---
    hcfg = cfg.raw.get("hook", {})
    if hcfg.get("enabled", False) and hook.strip():
        hsize = int(hcfg.get("font_size", 58) * scale)
        hprimary = _ass_color(hcfg.get("text_color", "#FFFFFF"))
        hbox = _ass_color(hcfg.get("box_color", "#E11D48"), hcfg.get("box_opacity", 0.85))
        hbold = -1 if hcfg.get("bold", True) else 0
        hside = int(hcfg.get("side_margin", 0.06) * w)
        halign = {"top": 8, "center": 5, "bottom": 2}.get(hcfg.get("position", "top"), 8)
        hmargin = int(hcfg.get("margin", 0.14) * h)
        hpad = max(10, int(18 * scale))
        styles.append(
            f"Style: Hook,{hcfg.get('font', 'Arial Black')},{hsize},{hprimary},"
            f"{hbox},&H00000000,{hbold},0,3,{hpad},0,{halign},{hside},{hside},{hmargin},1")
        htext = hook.strip()
        if hcfg.get("uppercase", True):
            htext = htext.upper()
        hmax = max(10, int((w - 2 * hside - 2 * hpad) / (hsize * 0.60)))
        hdur = float(hcfg.get("duration_seconds", 3) or 3)
        hook_end = _fmt_ass_time(min(hdur, clip_seconds))
        dialogues.append(
            f"Dialogue: 0,0:00:00.00,{hook_end},Hook,,0,0,0,,{_wrap(_esc(htext), hmax)}")

    # --- title card ---
    tcfg = cfg.raw.get("title", {})
    if tcfg.get("enabled", False) and title.strip():
        tsize = int(tcfg.get("font_size", 52) * scale)
        tprimary = _ass_color(tcfg.get("text_color", "#111111"))
        tbox = _ass_color(tcfg.get("box_color", "#FFFFFF"), tcfg.get("box_opacity", 1.0))
        tbold = -1 if tcfg.get("bold", True) else 0
        side = int(tcfg.get("side_margin", 0.06) * w)
        align = 2 if tcfg.get("position", "bottom") == "bottom" else 8
        margin = int(tcfg.get("margin", 0.15) * h)
        pad = max(10, int(tcfg.get("pad", 24) * scale))
        tshadow = max(0, int(tcfg.get("shadow", 8) * scale))
        tshadow_c = _ass_color(tcfg.get("shadow_color", "#000000"),
                               tcfg.get("shadow_opacity", 0.30))
        if tcfg.get("style", "card") in ("plain", "text"):
            # Plain burned text (no box) — text with an outline stroke, honours position.
            toutline = max(1, int(tcfg.get("outline", 5) * scale))
            toutline_c = _ass_color(tcfg.get("outline_color", "#000000"),
                                    tcfg.get("outline_opacity", 1.0))
            styles.append(
                f"Style: Title,{tcfg.get('font', 'Arial Black')},{tsize},{tprimary},"
                f"{toutline_c},{tshadow_c},{tbold},0,1,{toutline},{tshadow},{align},{side},{side},{margin},1")
        else:
            styles.append(
                f"Style: Title,{tcfg.get('font', 'Arial Black')},{tsize},{tprimary},"
                f"{tbox},{tshadow_c},{tbold},0,3,{pad},{tshadow},{align},{side},{side},{margin},1")
        avail = w - 2 * side - 2 * pad
        max_chars = max(10, int(avail / (tsize * 0.60)))
        prefix = tcfg.get("prefix", "").strip()
        ttext = f"{prefix} {title.strip()}".strip() if prefix else title.strip()
        if tcfg.get("uppercase", True):
            ttext = ttext.upper()
        if title_seconds is not None:
            title_end = _fmt_ass_time(min(float(title_seconds), clip_seconds))
        else:
            dur = tcfg.get("duration_seconds", 2)
            title_end = full_end if not dur else _fmt_ass_time(min(float(dur), clip_seconds))
        dialogues.append(
            f"Dialogue: 0,0:00:00.00,{title_end},Title,,0,0,0,,{_wrap(_esc(ttext), max_chars)}")

    # --- captions ---
    uppercase = cap.get("uppercase", True)
    highlight = cap.get("highlight", True)
    hi_c = _ass_inline(cap.get("highlight_color", "#FFE100"))
    base_c = _ass_inline(cap.get("text_color", "#FFFFFF"))

    def _tok(s: str) -> str:
        s = _esc(s)
        return s.upper() if uppercase else s

    if cap.get("enabled", True):
        # Suppress captions when there is no real dialogue: if speech covers less
        # than 5% of the clip it's almost certainly Whisper hallucination from the
        # initial_prompt or background noise — not actual words worth showing.
        speech_duration = sum(max(0.0, ev["end"] - ev["start"]) for ev in events)
        speech_fraction = speech_duration / clip_seconds if clip_seconds > 0 else 0
        if speech_fraction < 0.05:
            print(f"[captions] skipped — speech covers only "
                  f"{speech_fraction*100:.1f}% of clip (< 5%), likely noise")
        else:
            for idx, ev in enumerate(events):
                wlist = ev.get("words") or []
                # A line must clear the screen before the next one appears, or both
                # sit on screen at once and the new line stacks on top of the old.
                # Cap this line's end at the next line's start so captions show one at
                # a time (the reference channels all do this).
                next_start = events[idx + 1]["start"] if idx + 1 < len(events) else ev["end"]
                ev_end = min(ev["end"], next_start) if next_start > ev["start"] else ev["end"]
                if highlight and wlist:
                    # One event per spoken word: the whole line stays on screen while
                    # only the active word switches to the highlight colour. Events run
                    # back-to-back (word start -> next word start) so the line never
                    # flickers between highlights.
                    for i, wd in enumerate(wlist):
                        seg_s = wd["s"]
                        seg_e = wlist[i + 1]["s"] if i + 1 < len(wlist) else ev_end
                        seg_e = min(seg_e, next_start)  # never bleed into the next line
                        if seg_e <= seg_s:
                            seg_e = min(seg_s + 0.12, next_start)
                            if seg_e <= seg_s:  # non-monotonic ASR timing: drop, don't stack
                                continue
                        parts = []
                        for j, x in enumerate(wlist):
                            t = _tok(x["w"])
                            parts.append(f"{{\\c{hi_c}}}{t}{{\\c{base_c}}}" if j == i else t)
                        line = " ".join(parts)
                        dialogues.append(
                            f"Dialogue: 0,{_fmt_ass_time(seg_s)},{_fmt_ass_time(seg_e)},"
                            f"Cap,,0,0,0,,{line}")
                else:
                    txt = _tok(ev["text"])
                    dialogues.append(
                        f"Dialogue: 0,{_fmt_ass_time(ev['start'])},{_fmt_ass_time(ev_end)},"
                        f"Cap,,0,0,0,,{txt}")

    # --- watermark ---
    wcfg = cfg.raw.get("watermark", {})
    if wcfg.get("enabled", False) and wcfg.get("text", "").strip():
        wsize = int(wcfg.get("font_size", 34) * scale)
        wcol = _ass_color(wcfg.get("color", "#FFFFFF"), wcfg.get("opacity", 0.55))
        wout = _ass_color("#000000", wcfg.get("opacity", 0.55) * 0.8)
        wbold = -1 if wcfg.get("bold", False) else 0
        align = {"top": 8, "center": 5, "bottom": 2}.get(wcfg.get("position", "center"), 5)
        wmv = int(0.12 * h)
        styles.append(
            f"Style: Water,{wcfg.get('font', 'Arial')},{wsize},{wcol},{wout},"
            f"&H00000000,{wbold},0,1,2,0,{align},40,40,{wmv},1")
        dialogues.append(
            f"Dialogue: 0,0:00:00.00,{full_end},Water,,0,0,0,,{_esc(wcfg['text'])}")

    # --- outro / closing CTA (last N seconds) ---
    ocfg = cfg.raw.get("outro", {})
    _outro_text = outro_text.strip() or ocfg.get("text", "").strip()
    if ocfg.get("enabled", False) and _outro_text:
        osize = int(ocfg.get("font_size", 46) * scale)
        oprimary = _ass_color(ocfg.get("text_color", "#FFFFFF"))
        obox = _ass_color(ocfg.get("box_color", "#000000"), ocfg.get("box_opacity", 0.7))
        obold = -1 if ocfg.get("bold", True) else 0
        oalign = {"top": 8, "center": 5, "bottom": 2}.get(ocfg.get("position", "center"), 5)
        oside = int(0.08 * w)
        opad = max(10, int(16 * scale))
        omv = int(ocfg.get("margin", 0.12) * h)
        styles.append(
            f"Style: Outro,{ocfg.get('font', 'Arial')},{osize},{oprimary},{obox},"
            f"&H00000000,{obold},0,3,{opad},0,{oalign},{oside},{oside},{omv},1")
        odur_raw = ocfg.get("duration_seconds", 3)
        odur = float(odur_raw) if odur_raw not in (None, "") else 3.0
        # duration_seconds = 0  -> show for the whole clip (start at 0)
        if odur == 0:
            ostart = _fmt_ass_time(0.0)
        else:
            ostart = _fmt_ass_time(max(0.0, clip_seconds - odur))
        otext = _outro_text
        if ocfg.get("uppercase", False):
            otext = otext.upper()
        omax = max(10, int((w - 2 * oside - 2 * opad) / (osize * 0.60)))
        dialogues.append(
            f"Dialogue: 0,{ostart},{full_end},Outro,,0,0,0,,{_wrap(_esc(otext), omax)}")

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV, Encoding\n"
        + "\n".join(styles)
        + "\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    path.write_text(header + "\n".join(dialogues) + "\n", encoding="utf-8")


def _reframe_vf(mode: str, w: int, h: int, outro_start: float | None = None) -> str:
    """Base video filter that fits the source into a w x h frame.

    If `outro_start` is set, the main (sharp) video is hidden from that timestamp
    on: in blur mode only the blurred background remains; other modes fade to
    black. This makes the closing CTA card the sole focus.
    """
    if mode == "blur":
        base = (
            f"split=2[bg][fg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},gblur=sigma=20[bgb];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
        )
        overlay = "[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        if outro_start is not None:
            overlay += f":enable=lt(t\\,{outro_start:.3f})"  # drop fg during outro
        return base + overlay
    if mode == "contain":
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    else:  # default: crop (cover)
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    if outro_start is not None:
        vf += f",fade=t=out:st={outro_start:.3f}:d=0.5:color=black"
    return vf


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join(proc.stdout.splitlines()[-25:])
        raise RuntimeError(f"ffmpeg failed:\n{tail}")


def _write_main_content(video: Path, start: float, clip_dur: float, cfg: Config,
                        main_out: Path, *, vf: str | None = None,
                        base_video: Path | None = None) -> None:
    """Render the 'Main Content' deliverable: the same 9:16 framing as the final
    clip but RAW — no captions, no TTS hook, no title card, no outro. This is the
    editable reframe base a manual pass edits on top of. Either `vf` (a reframe
    base filter applied to the source span) or `base_video` (a pre-composited base,
    e.g. duosplit) is supplied; the clip's own audio (loudnorm'd) is always muxed.
    Reuses the crop path already computed for the final render — no second
    face-detection pass, only one extra encode."""
    if not cfg.ffmpeg:
        return
    main_out.parent.mkdir(parents=True, exist_ok=True)
    af = "loudnorm=I=-14:TP=-1.5:LRA=11"
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", str(main_out)]
    if base_video is not None:
        cmd = [cfg.ffmpeg, "-y", "-i", str(base_video),
               "-ss", f"{start:.3f}", "-t", f"{clip_dur:.3f}", "-i", str(video),
               "-map", "0:v:0", "-map", "1:a:0?", "-af", af, *enc]
    else:
        cmd = [cfg.ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(video),
               "-t", f"{clip_dur:.3f}", "-vf", vf, "-af", af, *enc]
    _run(cmd)


_HOOK_SENTENCE_END = (".", "!", "?", "…")


def _fallback_hook_anchor(words: list[dict], start: float, end: float) -> float:
    """Anchor for a cold open when the model didn't mark one: the start of a
    sentence near the clip's midpoint (where the point usually lands), falling
    back to the nearest word start, then to a fixed offset."""
    target = start + (end - start) * 0.45
    inwin = [w for w in words if w.get("start") is not None
             and start <= w["start"] <= end]
    if not inwin:
        return target
    sentence_starts = [inwin[0]["start"]]
    for prev, cur in zip(inwin, inwin[1:]):
        if prev["word"].strip().endswith(_HOOK_SENTENCE_END):
            sentence_starts.append(cur["start"])
    pool = sentence_starts or [w["start"] for w in inwin]
    return min(pool, key=lambda s: abs(s - target))


def _hook_segment(clip: dict, cfg: Config, start: float, end: float,
                  words: list[dict] | None = None) -> tuple[float, float] | None:
    """The cold-open window (absolute video seconds) inside [start, end], or None.

    A cold open is only worth it when the gripping line lives in the BACK of the
    clip — pulling it to the front then rewards the viewer for staying. When the
    hook already lands in the front half, relocating it just plays the same moment
    twice for no gain, so we return None and let the clip play raw. The front/back
    split is `relocate_after` (fraction of the clip; default 0.5). Uses the model's
    hook_start as the anchor when present, else a sentence near the clip midpoint.
    """
    hcfg = cfg.raw.get("hook", {})
    if not hcfg.get("enabled", False) or hcfg.get("mode", "clip") != "clip":
        return None
    lo = float(hcfg.get("clip_min_seconds", 3))
    hi = float(hcfg.get("clip_max_seconds", 6))
    if end - start < lo:
        return None  # clip itself too short to carve a cold open from
    try:
        h0 = float(clip["hook_start"])
        if not (start <= h0 <= end):
            raise ValueError
        try:
            h1 = float(clip["hook_end"])
        except (KeyError, TypeError, ValueError):
            h1 = h0
    except (KeyError, TypeError, ValueError):
        h0 = _fallback_hook_anchor(words or [], start, end)
        h1 = h0
    # Front/back gate: only relocate a hook that sits in the back of the clip.
    relocate_after = float(hcfg.get("relocate_after", 0.5))
    span = end - start
    if span > 0 and (h0 - start) / span < relocate_after:
        return None  # hook is already up front — play raw, no cold open
    # Grow a too-short mark up to `lo`, trim a too-long one to `hi`.
    want = min(max(h1 - h0, lo), hi)
    h0 = max(start, h0)
    h1 = h0 + want
    if h1 > end:  # ran off the end — slide the window back inside the clip
        h1 = end
        h0 = max(start, end - want)
    return round(h0, 3), round(h1, 3)


def render_clip(video: Path, clip: dict, transcript: dict, cfg: Config,
                out_path: Path, width: int, height: int,
                main_out: Path | None = None) -> Path:
    """Render one clip to `out_path` at width x height. Returns the path.

    When [hook] mode = "clip", the most gripping moment is copied to the front as
    a cold open, so the output runs hook_len + clip_len (the moment plays twice).

    When `main_out` is set, a RAW reframe-only cut (the 'Main Content' deliverable:
    same framing, no captions/hook/title/outro) is also written there, reusing the
    crop path computed for the final render.
    """
    if not cfg.ffmpeg:
        raise RuntimeError("ffmpeg not found. Run `python -m clipcraft doctor`.")

    start = float(clip["start"])
    end = float(clip["end"])
    clip_dur = round(end - start, 3)
    w, h = width, height

    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent

    # Dynamic duo-split: build the two-panel/single base, then finish it (captions,
    # title, watermark, outro, TTS hook) exactly like a user-framed raw cut — no
    # reframe on top, since the base is already the final framing.
    if cfg.clip.get("fit") == "duosplit":
        from . import duosplit as _ds
        base = _ds.build_base(video, start, end, w, h, cfg, clip=clip)
        if main_out is not None:
            try:
                _write_main_content(video, start, clip_dur, cfg, main_out,
                                    base_video=base)
            except Exception as exc:
                print(f"[main] {main_out.name} failed: {exc}")
        return render_finish(base, clip, cfg, out_path, w, h,
                             transcript.get("words", []), start, end)

    body = None          # reframe crop path, filled when fit == "reframe"
    events: list[dict] = []  # caption events, filled below; reused by editor export

    hook_seg = _hook_segment(clip, cfg, start, end, transcript.get("words", []))
    hook_dur = round(hook_seg[1] - hook_seg[0], 3) if hook_seg else 0.0

    # Freeze-blur TTS title intro: synth the voice first so we know how long to hold
    # the opening frame. The hold+blur is applied to the VIDEO only; the title card
    # is burned on top afterward, so it stays sharp. Mutually exclusive with the
    # cold-open hook.
    from . import tts
    ttscfg = cfg.raw.get("tts", {})
    want_freeze = (ttscfg.get("enabled", False)
                   and bool(ttscfg.get("hold_first_frame", True)) and not hook_seg)
    if want_freeze and not tts.available():
        print("[tts] edge-tts not installed — run `pip install edge-tts`. "
              "Rendering without the title voiceover.")
    freeze_on = want_freeze and tts.available()
    voice_mp3 = None
    window = 0.0
    if freeze_on:
        vtext = tts._voice_text(clip, ttscfg.get("source", "hook"))
        cand = work / (out_path.stem + ".voice.mp3")
        if vtext and tts.synthesize(vtext, cand, cfg) is not None:
            vdur = tts._probe_duration(cfg.ffprobe, cand)
            if vdur > 0:
                window = round(vdur + float(ttscfg.get("lead_in", 0.15) or 0.0), 3)
                voice_mp3 = cand

    total = round(hook_dur + window + clip_dur, 3)

    ocfg = cfg.raw.get("outro", {})
    outro_on = ocfg.get("enabled", False) and ocfg.get("text", "").strip()
    odur_raw = ocfg.get("duration_seconds", 3)
    odur = float(odur_raw) if odur_raw not in (None, "") else 3.0
    # duration_seconds = 0 -> full-clip overlay: starts at 0, never hides video
    if outro_on:
        outro_start = 0.0 if odur == 0 else max(0.0, total - odur)
    else:
        outro_start = None
    hide_video = bool(ocfg.get("hide_video", True)) and odur != 0
    fade_audio = bool(ocfg.get("fade_audio", True)) and odur != 0

    fit = cfg.clip.get("fit", "crop")
    from . import triptych as _tp
    use_triptych = fit in ("triptych", "stack") and _tp.enabled(cfg)
    if fit == "triptych":
        fit = "reframe"                    # base = face-reframe; stack overlays on detect
    elif fit == "stack":
        fit = "crop"                       # base is hidden under an always-on stack: keep cheap
    phone_spans: list[tuple[float, float]] = []
    if use_triptych:
        phone_spans = _tp.resolve_spans(video, start, end, cfg, clip_dur)
        print(f"[stack] {out_path.name} spans (clip-local): "
              f"{phone_spans or 'none — plain base'}")
        if not phone_spans:
            use_triptych = False
        elif hook_seg:
            print("[stack] cold-open hook active for this clip; stack overlay "
                  "skipped (not yet supported with a relocated hook).")
            use_triptych = False
    # Manual per-clip reaction split: overlay a configured [stack] layout ONLY
    # during the given clip-local windows (content-local seconds; the freeze intro
    # offset is applied downstream), on top of the normal reframe base — regardless
    # of fit/detection. Used to force a duo/trio split on a group-reaction moment
    # that a single crop would waste. Needs [stack].panels for the regions.
    manual_windows = clip.get("stack_windows")
    if manual_windows and cfg.raw.get("stack", {}).get("panels") and not hook_seg:
        use_triptych = True
        phone_spans = [(float(a), float(b)) for a, b in manual_windows]
        print(f"[stack] {out_path.name} manual stack_windows: {phone_spans}")
    reframe_hook = None
    if fit == "reframe":
        from . import reframe as _rf
        ropts = cfg.raw.get("reframe", {})
        _ofade = outro_start if (outro_on and hide_video) else None
        if ropts.get("mode") == "asd":
            from . import asd as _asd
            body = _asd.compute_asd_crop_path(video, start, end, w, h, cfg, ropts)
        elif ropts.get("mode") == "speaker" and cfg.raw.get("diarize", {}).get("enabled"):
            body = _rf.compute_speaker_crop_path(video, start, end, w, h, cfg, ropts)
        else:
            body = _rf.compute_crop_path(video, start, end, w, h, ropts)
        locks = clip.get("reframe_locks")
        if locks:
            body["kfs"] = _rf.apply_locks(body["kfs"], locks, body)
        st = body.get("stats", {})
        print(f"[reframe] {out_path.name} faces: single {st.get('single', 0)} "
              f"two-shot {st.get('two', 0)} none {st.get('none', 0)}")

        def _mk(segments):
            vf = _rf.crop_filter(body, segments, w, h)
            if _ofade is not None:
                vf += f",fade=t=out:st={_ofade:.3f}:d=0.5:color=black"
            return vf

        reframe = _mk([(body["kfs"], clip_dur)])
        if hook_seg:
            hk = _rf.compute_crop_path(video, hook_seg[0], hook_seg[1], w, h, ropts)
            reframe_hook = _mk([(hk["kfs"], hook_dur), (body["kfs"], clip_dur)])
    else:
        reframe = _reframe_vf(fit, w, h,
                              outro_start=outro_start if (outro_on and hide_video) else None)

    # Main Content deliverable: the reframe base with NO outro fade, captions, hook,
    # title, or freeze intro. Emitted from the crop path just computed, so it costs
    # one extra encode and no second detection pass.
    if main_out is not None:
        if fit == "reframe":
            main_vf = _rf.crop_filter(body, [(body["kfs"], clip_dur)], w, h)
        else:
            main_vf = _reframe_vf(fit, w, h, outro_start=None)
        try:
            _write_main_content(video, start, clip_dur, cfg, main_out, vf=main_vf)
        except Exception as exc:
            print(f"[main] {main_out.name} failed: {exc}")

    hcfg = cfg.raw.get("hook", {})
    captions_on = cfg.captions.get("enabled", True)
    title_on = cfg.raw.get("title", {}).get("enabled", False) and clip.get("title")
    watermark_on = cfg.raw.get("watermark", {}).get("enabled", False)
    text_hook_on = (hcfg.get("enabled", False) and hcfg.get("mode", "clip") == "text"
                    and clip.get("hook"))

    tcfg = cfg.raw.get("title", {})
    title_card = None
    if title_on and tcfg.get("style", "card") == "card":
        from . import titlecard
        tc_path = work / (out_path.stem + ".title.png")
        try:
            title_card = titlecard.render_card(clip.get("title", ""), w, cfg, tc_path)
        except Exception as exc:
            print(f"[titlecard] render failed ({exc}); using box title.")
            title_card = None

    ass_name = None
    if captions_on or title_on or watermark_on or outro_on or text_hook_on:
        wpl = int(cfg.captions.get("words_per_line", 4) or 4)
        words = transcript.get("words", [])
        events: list[dict] = []
        if captions_on:
            if hook_seg:
                events += _group_words(words, hook_seg[0], hook_seg[1], max_words=wpl)
                for e in _group_words(words, start, end, max_words=wpl):
                    shifted = [{"s": x["s"] + hook_dur, "e": x["e"] + hook_dur,
                                "w": x["w"]} for x in e.get("words", [])]
                    events.append({"start": e["start"] + hook_dur,
                                   "end": e["end"] + hook_dur, "text": e["text"],
                                   "words": shifted})
            else:
                events = _group_words(words, start, end, max_words=wpl)
                if window > 0:  # freeze intro shifts the whole clip back by `window`
                    for e in events:
                        e["start"] += window
                        e["end"] += window
                        for x in e.get("words", []):
                            x["s"] += window
                            x["e"] += window
        from .select import has_cjk
        if any(has_cjk(e.get("text", "")) for e in events):
            print(f"[render] WARNING: {out_path.name} has Chinese/CJK text in its "
                  "captions — the transcript for this span is misdetected. Fix it "
                  "with:  py -3 -m clipcraft recut --project <name> --clip <n>")
        ass_path = work / (out_path.stem + ".ass")
        _write_ass(events, cfg, ass_path, w, h,
                   title="" if title_card else clip.get("title", ""),
                   clip_seconds=total,
                   hook=clip.get("hook", "") if text_hook_on else "",
                   outro_text="")
        ass_name = ass_path.name

    # Editor-project export (DaVinci/Premiere FCPXML, OTIO, CapCut) — alongside
    # the MP4, reusing the crop path + caption events + TTS we just computed.
    from . import export as _ex
    if _ex.enabled(cfg):
        try:
            if hook_seg:
                vsegs = [(0.0, hook_seg[0], hook_dur),
                         (hook_dur, start, clip_dur)]
                tts_start = 0.0
            elif window > 0:
                vsegs = [(0.0, start, window), (window, start, clip_dur)]
                tts_start = float(ttscfg.get("lead_in", 0.15) or 0.0)
            else:
                vsegs = [(0.0, start, clip_dur)]
                tts_start = 0.0
            man = _ex.build_manifest(
                clip=clip, cfg=cfg, video=video, out_stem=out_path.stem,
                out_w=w, out_h=h, total=total, src_start=start, src_end=end,
                video_segments=vsegs, crop=body, events=events,
                title_text=clip.get("title", "") if title_on else "",
                tts_path=voice_mp3, tts_start=tts_start)
            edit_dir = work.parent / "edit"
            wrote = _ex.export_clip(man, edit_dir, cfg)
            if wrote:
                print(f"[export] {out_path.stem}: {', '.join(Path(p).name for p in wrote)}")
        except Exception as exc:
            print(f"[export] skipped for {out_path.stem}: {exc}")

    af = "loudnorm=I=-14:TP=-1.5:LRA=11"
    if outro_on and fade_audio and outro_start is not None:
        af += f",afade=t=out:st={outro_start:.3f}:d={odur:.3f}"

    subs = f",subtitles={ass_name}" if ass_name else ""
    vchain = reframe + subs
    common_out = [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(out_path),
    ]

    # An extra raw decode of the clip window, appended as the LAST ffmpeg input,
    # is the source the triptych is composed from and overlaid onto the base.
    trip_input = (["-ss", f"{start:.3f}", "-t", f"{clip_dur:.3f}", "-i", str(video)]
                  if use_triptych else [])

    def _sub_then_out(cur: str) -> str:
        """Burn captions (if any) after the triptych overlay so they stay on top,
        ending at [outv]."""
        if ass_name:
            return f"[{cur}]subtitles={ass_name}[outv]"
        return f"[{cur}]null[outv]"

    # Rounded title card is a looped image input overlaid at the bottom for its
    # duration. `card_idx` is filled per branch (inputs before it differ).
    def _card_overlay(prev: str, card_idx: int, vc: str) -> str:
        tdur = float(tcfg.get("duration_seconds", 2) or 0)
        tmv = int(float(tcfg.get("margin", 0.15)) * h)
        en = f":enable='lt(t\\,{tdur:.3f})'" if tdur > 0 else ""
        return (f"[{prev}]{vc}[vb];"
                f"[vb][{card_idx}:v]overlay=(W-w)/2:H-h-{tmv}{en}[outv]")

    if voice_mp3 is not None and window > 0:
        # Freeze-blur TTS title intro. Blur is applied to the VIDEO only (before the
        # title card is overlaid), so the card stays sharp while the voice reads it.
        sigma = float(ttscfg.get("blur_sigma", 24) or 0)
        gain = float(ttscfg.get("gain", 1.0))
        lead_ms = int(round(float(ttscfg.get("lead_in", 0.15) or 0.0) * 1000))
        window_ms = int(round(window * 1000))
        blur = (f",gblur=sigma={sigma}:enable='lt(t\\,{window:.3f})'"
                if sigma > 0 else "")
        if fit == "reframe":  # hold the first body crop over the frozen intro
            reframe = _mk([([(0.0, body["kfs"][0][1])], window), (body["kfs"], clip_dur)])
        # tpad clones the raw source frame 0 BEFORE reframe, so the reframe re-composites
        # each held frame (sharp foreground over blurred bars). Cloning AFTER the blur-mode
        # overlay would drop the sharp foreground and leave only the blurred background.
        tpad = f"tpad=start_duration={window:.3f}:start_mode=clone,"
        card_in = ["-loop", "1", "-t", f"{total:.3f}", "-i", str(title_card)] \
            if title_card else []
        voice_idx = 2 if title_card else 1
        tdur = float(tcfg.get("duration_seconds", 2) or 0)
        tmv = int(float(tcfg.get("margin", 0.15)) * h)
        title_end = (window + tdur) if tdur > 0 else 0.0
        ten = f":enable='lt(t\\,{title_end:.3f})'" if title_end > 0 else ""
        if use_triptych:
            # Base reframe (blurred frozen intro), NO captions yet; overlay the
            # triptych during the phone spans (shifted by the intro window); THEN
            # burn captions and the title card so both stay above the triptych.
            trip_idx = voice_idx + 1
            base_chain = tpad + reframe + blur
            parts = [f"[0:v]{base_chain}[tp_base]",
                     _tp.overlay_tail("tp_base", "tp_cmp", f"{trip_idx}:v",
                                      phone_spans, cfg, w, h, offset=window,
                                      pre=tpad.rstrip(","))]
            cur = "tp_cmp"
            if title_card:
                if ass_name:
                    parts.append(f"[{cur}]subtitles={ass_name}[tp_sub]")
                    cur = "tp_sub"
                parts.append(f"[{cur}][1:v]overlay=(W-w)/2:H-h-{tmv}{ten}[outv]")
            else:
                parts.append(_sub_then_out(cur))
            vpost = ";".join(parts)
        elif title_card:
            freeze_chain = tpad + reframe + blur + subs
            vpost = (f"[0:v]{freeze_chain}[vb];"
                     f"[vb][1:v]overlay=(W-w)/2:H-h-{tmv}{ten}[outv]")
        else:
            freeze_chain = tpad + reframe + blur + subs
            vpost = f"[0:v]{freeze_chain}[outv]"
        aud = (f"[0:a]adelay={window_ms}:all=1[a0];"
               f"[{voice_idx}:a]adelay={lead_ms}:all=1,volume={gain}[vo];"
               f"[a0][vo]amix=inputs=2:duration=longest:dropout_transition=0:"
               f"normalize=0,{af}[outa]")
        cmd = [
            cfg.ffmpeg, "-y",
            "-ss", f"{start:.3f}", "-t", f"{clip_dur:.3f}", "-i", str(video),
        ] + card_in + [
            "-i", str(voice_mp3),
        ] + trip_input + [
            "-filter_complex", vpost + ";" + aud,
            "-map", "[outv]", "-map", "[outa]",
        ] + common_out
        print(f"[render] {out_path.name}  freeze-{'blur' if sigma > 0 else 'hold'} "
              f"title {window:.1f}s + {start:.2f}-{end:.2f} = {total:.1f}s"
              + (f"  +stack x{len(phone_spans)}" if use_triptych else ""))
    elif hook_seg:
        h0, h1 = hook_seg
        vchain_h = (reframe_hook + (f",subtitles={ass_name}" if ass_name else "")
                    if reframe_hook else vchain)
        card_in = (["-loop", "1", "-t", f"{total:.3f}", "-i", str(title_card)]
                   if title_card else [])
        vpost = (_card_overlay("cv", 2, vchain_h) if title_card
                 else f"[cv]{vchain_h}[outv]")
        fc = (
            "[0:v]setpts=PTS-STARTPTS[v0];[0:a]asetpts=PTS-STARTPTS[a0];"
            "[1:v]setpts=PTS-STARTPTS[v1];[1:a]asetpts=PTS-STARTPTS[a1];"
            "[v0][a0][v1][a1]concat=n=2:v=1:a=1[cv][ca];"
            f"{vpost};[ca]{af}[outa]"
        )
        cmd = [
            cfg.ffmpeg, "-y",
            "-ss", f"{h0:.3f}", "-t", f"{hook_dur:.3f}", "-i", str(video),
            "-ss", f"{start:.3f}", "-t", f"{clip_dur:.3f}", "-i", str(video),
        ] + card_in + [
            "-filter_complex", fc, "-map", "[outv]", "-map", "[outa]",
        ] + common_out
        print(f"[render] {out_path.name}  cold-open {h0:.2f}-{h1:.2f} + "
              f"{start:.2f}-{end:.2f} = {total:.1f}s")
    elif title_card:
        if use_triptych:
            tdur = float(tcfg.get("duration_seconds", 2) or 0)
            tmv = int(float(tcfg.get("margin", 0.15)) * h)
            en = f":enable='lt(t\\,{tdur:.3f})'" if tdur > 0 else ""
            parts = [f"[0:v]{reframe}[tp_base]",
                     _tp.overlay_tail("tp_base", "tp_cmp", "2:v", phone_spans,
                                      cfg, w, h)]
            cur = "tp_cmp"
            if ass_name:
                parts.append(f"[{cur}]subtitles={ass_name}[tp_sub]")
                cur = "tp_sub"
            parts.append(f"[{cur}][1:v]overlay=(W-w)/2:H-h-{tmv}{en}[outv]")
            fc = ";".join(parts)
        else:
            fc = _card_overlay("0:v", 1, vchain)
        cmd = [
            cfg.ffmpeg, "-y",
            "-ss", f"{start:.3f}", "-t", f"{clip_dur:.3f}", "-i", str(video),
            "-loop", "1", "-t", f"{clip_dur:.3f}", "-i", str(title_card),
        ] + trip_input + [
            "-filter_complex", fc, "-map", "[outv]", "-map", "0:a", "-af", af,
        ] + common_out
        print(f"[render] {out_path.name}  {start:.2f}-{end:.2f} ({clip_dur:.1f}s)"
              + (f"  +stack x{len(phone_spans)}" if use_triptych else ""))
    elif use_triptych:
        fc = (f"[0:v]{reframe}[tp_base];"
              + _tp.overlay_tail("tp_base", "tp_cmp", "1:v", phone_spans, cfg, w, h)
              + ";" + _sub_then_out("tp_cmp"))
        cmd = [
            cfg.ffmpeg, "-y",
            "-ss", f"{start:.3f}", "-t", f"{clip_dur:.3f}", "-i", str(video),
        ] + trip_input + [
            "-filter_complex", fc, "-map", "[outv]", "-map", "0:a", "-af", af,
        ] + common_out
        print(f"[render] {out_path.name}  {start:.2f}-{end:.2f} ({clip_dur:.1f}s)"
              f"  +stack x{len(phone_spans)}")
    else:
        cmd = [
            cfg.ffmpeg, "-y",
            "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{clip_dur:.3f}",
            "-vf", vchain, "-af", af,
        ] + common_out
        print(f"[render] {out_path.name}  {start:.2f}-{end:.2f} ({clip_dur:.1f}s)")
    _run(cmd, cwd=work)
    if not freeze_on:  # talk-over mode still post-processes; freeze is done inline
        tts.apply_hook_voiceover(out_path, clip, cfg, work)
    return out_path


def render_finish(clip_file: Path, clip: dict, cfg: Config, out_path: Path,
                  width: int, height: int, words: list[dict],
                  win_start: float, win_end: float) -> Path:
    """Re-render a clip the user has already framed themselves ("focus" pass):
    add the TTS hook intro, burned captions, title card, outro and watermark —
    but NO reframe (the crop is the user's own) and NO cold-open (that needs the
    master; on a baked clip the hook is TTS voiceover over the opening seconds).

    `clip_file` is the user's vertical clip. `words` + [win_start, win_end] locate
    the caption words in that clip's own timebase (see cmd_finish for how the two
    cases — reuse-master vs. re-transcribe — resolve them).
    """
    if not cfg.ffmpeg:
        raise RuntimeError("ffmpeg not found. Run `python -m clipcraft doctor`.")

    w, h = width, height
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent
    clip_dur = round(win_end - win_start, 3)

    # TTS freeze-blur intro: reuse the pre-synthesized voice from the raw export
    # when present, else synth now. Same mechanic as render_clip's freeze branch.
    from . import tts
    ttscfg = cfg.raw.get("tts", {})
    freeze_on = (ttscfg.get("enabled", False)
                 and bool(ttscfg.get("hold_first_frame", True)) and tts.available())
    voice_mp3 = None
    window = 0.0
    if freeze_on:
        cand = work / (out_path.stem + ".voice.mp3")
        pre = clip.get("tts")
        if pre and Path(pre).exists():
            import shutil
            shutil.copyfile(pre, cand)
        else:
            vtext = tts._voice_text(clip, ttscfg.get("source", "hook"))
            if vtext:
                tts.synthesize(vtext, cand, cfg)
        if cand.exists() and cand.stat().st_size > 0:
            vdur = tts._probe_duration(cfg.ffprobe, cand)
            if vdur > 0:
                window = round(vdur + float(ttscfg.get("lead_in", 0.15) or 0.0), 3)
                voice_mp3 = cand

    total = round(window + clip_dur, 3)

    ocfg = cfg.raw.get("outro", {})
    ocfg = cfg.raw.get("outro", {})
    outro_on = ocfg.get("enabled", False) and ocfg.get("text", "").strip()
    odur_raw = ocfg.get("duration_seconds", 3)
    odur = float(odur_raw) if odur_raw not in (None, "") else 3.0
    # duration_seconds = 0 -> full-clip overlay: starts at 0, never hides video
    if outro_on:
        outro_start = 0.0 if odur == 0 else max(0.0, total - odur)
    else:
        outro_start = None
    hide_video = bool(ocfg.get("hide_video", True)) and odur != 0
    fade_audio = bool(ocfg.get("fade_audio", True)) and odur != 0
    ofade = (f",fade=t=out:st={outro_start:.3f}:d=0.5:color=black"
             if (outro_on and hide_video and outro_start is not None) else "")

    # Base fit: cover-crop to the frame. On an already-9:16 clip this is a no-op,
    # so the user's own framing is preserved untouched.
    reframe = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"

    hcfg = cfg.raw.get("hook", {})
    captions_on = cfg.captions.get("enabled", True)
    title_on = cfg.raw.get("title", {}).get("enabled", False) and clip.get("title")
    watermark_on = cfg.raw.get("watermark", {}).get("enabled", False)
    text_hook_on = (hcfg.get("enabled", False) and hcfg.get("mode", "clip") == "text"
                    and clip.get("hook"))

    tcfg = cfg.raw.get("title", {})
    title_card = None
    if title_on and tcfg.get("style", "card") == "card":
        from . import titlecard
        tc_path = work / (out_path.stem + ".title.png")
        try:
            title_card = titlecard.render_card(clip.get("title", ""), w, cfg, tc_path)
        except Exception as exc:
            print(f"[titlecard] render failed ({exc}); using box title.")
            title_card = None

    ass_name = None
    if captions_on or title_on or watermark_on or outro_on or text_hook_on:
        wpl = int(cfg.captions.get("words_per_line", 4) or 4)
        events = _group_words(words, win_start, win_end, max_words=wpl) if captions_on else []
        if window > 0:  # freeze intro shifts the whole clip back by `window`
            for e in events:
                e["start"] += window
                e["end"] += window
                for x in e.get("words", []):
                    x["s"] += window
                    x["e"] += window
        from .select import has_cjk
        if any(has_cjk(e.get("text", "")) for e in events):
            print(f"[finish] WARNING: {out_path.name} has CJK text in captions — "
                  "the transcript span is misdetected; fix the transcript sidecar.")
        ass_path = work / (out_path.stem + ".ass")
        _write_ass(events, cfg, ass_path, w, h,
                   title="" if title_card else clip.get("title", ""),
                   clip_seconds=total,
                   hook=clip.get("hook", "") if text_hook_on else "",
                   outro_text="")
        ass_name = ass_path.name

    af = "loudnorm=I=-14:TP=-1.5:LRA=11"
    if outro_on and fade_audio and outro_start is not None:
        af += f",afade=t=out:st={outro_start:.3f}:d={odur:.3f}"
    subs = f",subtitles={ass_name}" if ass_name else ""
    common_out = [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(out_path),
    ]
    tdur = float(tcfg.get("duration_seconds", 2) or 0)
    tmv = int(float(tcfg.get("margin", 0.15)) * h)

    if voice_mp3 is not None and window > 0:
        sigma = float(ttscfg.get("blur_sigma", 24) or 0)
        gain = float(ttscfg.get("gain", 1.0))
        lead_ms = int(round(float(ttscfg.get("lead_in", 0.15) or 0.0) * 1000))
        window_ms = int(round(window * 1000))
        blur = (f",gblur=sigma={sigma}:enable='lt(t\\,{window:.3f})'"
                if sigma > 0 else "")
        tpad = f"tpad=start_duration={window:.3f}:start_mode=clone,"
        freeze_chain = tpad + reframe + blur + ofade + subs
        card_in = (["-loop", "1", "-t", f"{total:.3f}", "-i", str(title_card)]
                   if title_card else [])
        voice_idx = 2 if title_card else 1
        title_end = (window + tdur) if tdur > 0 else 0.0
        ten = f":enable='lt(t\\,{title_end:.3f})'" if title_end > 0 else ""
        if title_card:
            vpost = (f"[0:v]{freeze_chain}[vb];"
                     f"[vb][1:v]overlay=(W-w)/2:H-h-{tmv}{ten}[outv]")
        else:
            vpost = f"[0:v]{freeze_chain}[outv]"
        aud = (f"[0:a]adelay={window_ms}:all=1[a0];"
               f"[{voice_idx}:a]adelay={lead_ms}:all=1,volume={gain}[vo];"
               f"[a0][vo]amix=inputs=2:duration=longest:dropout_transition=0:"
               f"normalize=0,{af}[outa]")
        cmd = [cfg.ffmpeg, "-y", "-i", str(clip_file)] + card_in + [
            "-i", str(voice_mp3),
            "-filter_complex", vpost + ";" + aud,
            "-map", "[outv]", "-map", "[outa]",
        ] + common_out
        print(f"[finish] {out_path.name}  freeze-{'blur' if sigma > 0 else 'hold'} "
              f"title {window:.1f}s + clip {clip_dur:.1f}s = {total:.1f}s")
    else:
        vchain = reframe + ofade + subs
        if title_card:
            en = f":enable='lt(t\\,{tdur:.3f})'" if tdur > 0 else ""
            fc = (f"[0:v]{vchain}[vb];"
                  f"[vb][1:v]overlay=(W-w)/2:H-h-{tmv}{en}[outv]")
            cmd = [cfg.ffmpeg, "-y", "-i", str(clip_file),
                   "-loop", "1", "-t", f"{clip_dur:.3f}", "-i", str(title_card),
                   "-filter_complex", fc, "-map", "[outv]", "-map", "0:a",
                   "-af", af] + common_out
        else:
            cmd = [cfg.ffmpeg, "-y", "-i", str(clip_file),
                   "-vf", vchain, "-af", af] + common_out
        print(f"[finish] {out_path.name}  clip {clip_dur:.1f}s (no TTS intro)")
    _run(cmd, cwd=work)
    return out_path
