"""Stacked split-screen layouts (triptych, duo, streamer) + dynamic switching.

A whole family of vertical layouts is just an ordered stack of panels, where each
panel crops a region of the 16:9 source into a full-width band:

    triptych  [face | phone  | face]   two reactions flanking app content
    duo       [face | face]            two people talking at once
    streamer  [face | gameplay]        facecam over the game

All three are the same engine: `stack_subgraph()` composes a panels list into a
w x h vertical stack. A panel is `cover` (crop to fill the band) or `contain`
(fit whole, blurred fill) — content panels (a phone, a game) want `contain` so
nothing is lost; face panels want `cover`.

Two ways to trigger a stack:

* **Always on** (`fit="stack"`, no detect): the stack is the whole clip. Used for
  duo / streamer where the layout never changes.
* **Content switch** (`fit="triptych"`): the base is normal face-reframe, and the
  stack is overlaid ONLY while the app-demo layout is on screen — detected from
  the sponsor banner's colour. See `detect_phone_spans()`.

Regions are per-source (they depend on where that editor placed things), so they
live in `[triptych]` / `[stack]` in the project config. Fixed regions work when
the source is a stable composite (the trading app demo) or a locked camera angle;
a live multi-cam with small, moving faces needs per-angle regions or a stronger
face model — fixed crops alone won't follow the speakers.
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("GLOG_minloglevel", "3")

import cv2  # noqa: E402

# Triptych defaults measured on the "Live Trading 5 Miliar" source (1920x1080).
_DEF_TOP = {"x": 160, "y": 378, "w": 665, "h": 327}      # left reaction face
_DEF_BOTTOM = {"x": 1225, "y": 470, "w": 665, "h": 327}  # right reaction face
_DEF_PHONE = {"x": 735, "y": 70, "w": 445, "h": 915}     # centre phone
_DEF_BANDS = {"top": 520, "mid": 880, "bottom": 520}     # output band heights
_DEF_REGION = {"x": 760, "y": 1015, "w": 400, "h": 50}   # banner sample area


def _rect(d: dict, key: str, default: dict) -> tuple[int, int, int, int]:
    r = d.get(key, default) or default
    return int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])


def enabled(cfg) -> bool:
    return bool(cfg.raw.get("triptych", {}).get("enabled", False)
               or cfg.raw.get("stack", {}).get("enabled", False))


def _triptych_panels(cfg) -> list[dict]:
    """The classic face/phone/face panels from the [triptych] config."""
    t = cfg.raw.get("triptych", {})
    b = t.get("bands", _DEF_BANDS)
    sigma = float(t.get("blur_sigma", 25.0))
    tx, ty, tw, th = _rect(t, "top", _DEF_TOP)
    px, py, pw, ph = _rect(t, "phone", _DEF_PHONE)
    bx, by, bw, bh = _rect(t, "bottom", _DEF_BOTTOM)
    return [
        {"rect": (tx, ty, tw, th), "height": int(b.get("top", 520)), "mode": "cover"},
        {"rect": (px, py, pw, ph), "height": int(b.get("mid", 880)),
         "mode": "contain", "blur": sigma},
        {"rect": (bx, by, bw, bh), "height": int(b.get("bottom", 520)), "mode": "cover"},
    ]


def _stack_panels(cfg) -> list[dict]:
    """Panels from the generic [stack] config (array of tables)."""
    out = []
    for p in cfg.raw.get("stack", {}).get("panels", []):
        r = p.get("rect", {})
        out.append({
            "rect": (int(r["x"]), int(r["y"]), int(r["w"]), int(r["h"])),
            "height": int(p.get("height", 0)) or 1,
            "mode": p.get("mode", "cover"),
            "blur": float(p.get("blur", 25.0)),
        })
    return out


def panels(cfg) -> list[dict]:
    """Resolve the panels for this project: explicit [stack].panels, else the
    [triptych] preset."""
    if cfg.raw.get("stack", {}).get("panels"):
        return _stack_panels(cfg)
    return _triptych_panels(cfg)


def _scaled_heights(pnls: list[dict], out_h: int) -> list[int]:
    """Panel heights scaled to fill out_h exactly (last panel absorbs rounding)."""
    total = sum(max(1, p["height"]) for p in pnls) or 1
    hs = [int(round(out_h * max(1, p["height"]) / total / 2) * 2) for p in pnls]
    hs[-1] = out_h - sum(hs[:-1])
    return hs


def stack_subgraph(in_lbl: str, out_lbl: str, cfg, w: int, h: int) -> str:
    """Filtergraph consuming raw source `[in_lbl]` and producing the vertical
    stack `[out_lbl]` at w x h. Crop coords are in SOURCE pixels."""
    pnls = panels(cfg)
    hs = _scaled_heights(pnls, h)
    n = len(pnls)
    parts = [f"[{in_lbl}]split={n}" + "".join(f"[k{i}]" for i in range(n))]
    band_lbls = []
    for i, (p, bh) in enumerate(zip(pnls, hs)):
        x, y, cw, ch = p["rect"]
        bl = f"kb{i}"
        if p.get("mode") == "contain":
            sigma = float(p.get("blur", 25.0))
            parts.append(
                f"[k{i}]crop={cw}:{ch}:{x}:{y},split=2[k{i}a][k{i}b];"
                f"[k{i}a]scale={w}:{bh}:force_original_aspect_ratio=increase,"
                f"crop={w}:{bh},gblur=sigma={sigma}[k{i}bg];"
                f"[k{i}b]scale={w}:{bh}:force_original_aspect_ratio=decrease[k{i}fg];"
                f"[k{i}bg][k{i}fg]overlay=(W-w)/2:(H-h)/2[{bl}]")
        else:  # cover: crop to band aspect, then fill
            parts.append(
                f"[k{i}]crop={cw}:{ch}:{x}:{y},"
                f"scale={w}:{bh}:force_original_aspect_ratio=increase,"
                f"crop={w}:{bh},setsar=1[{bl}]")
        band_lbls.append(bl)
    if n == 1:  # a single full-frame panel ("pull out to wide"): no vstack
        parts.append(f"[{band_lbls[0]}]setsar=1[{out_lbl}]")
    else:
        parts.append("".join(f"[{b}]" for b in band_lbls)
                     + f"vstack=inputs={n},setsar=1[{out_lbl}]")
    return ";".join(parts)


# --- banner-based content detection (triptych mode) ---------------------------

def detect_phone_spans(video: Path, start: float, end: float,
                       cfg) -> list[tuple[float, float]]:
    """Spans (clip-local seconds) where the app-demo banner is on screen.
    Consecutive hits are merged across small gaps, short blips dropped, and each
    span padded slightly so the switch doesn't clip the cut."""
    t = cfg.raw.get("triptych", {})
    det = t.get("detect", {})
    rx, ry, rw, rh = _rect(det, "region", _DEF_REGION)
    sample_fps = float(det.get("sample_fps", 4.0))
    g_over_r = float(det.get("green_over_red", 20.0))
    min_g = float(det.get("min_green", 60.0))
    max_r = float(det.get("max_red", 130.0))
    min_dur = float(det.get("min_seconds", 1.2))
    merge_gap = float(det.get("merge_gap", 1.0))
    pad = float(det.get("pad", 0.12))

    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    step = max(1, round(fps / sample_fps))
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(start * fps))
    fidx, end_f = int(start * fps), int(end * fps)

    y0, y1 = max(0, ry), min(src_h, ry + rh)
    x0, x1 = max(0, rx), min(src_w, rx + rw)
    samples: list[tuple[float, bool]] = []
    dt = step / fps
    while fidx <= end_f:
        ok, frame = cap.read()
        if not ok:
            break
        sub = frame[y0:y1, x0:x1]
        if sub.size:
            b = float(sub[..., 0].mean())
            g = float(sub[..., 1].mean())
            r = float(sub[..., 2].mean())
            phone = (g - r) > g_over_r and g > min_g and r < max_r
        else:
            phone = False
        samples.append((fidx / fps - start, phone))
        for _ in range(step - 1):
            cap.grab()
            fidx += 1
        fidx += 1
    cap.release()

    spans: list[list[float]] = []
    run_start = None
    for i, (tt, on) in enumerate(samples):
        if on and run_start is None:
            run_start = tt
        if (not on or i == len(samples) - 1) and run_start is not None:
            run_end = tt if on else samples[i - 1][0] + dt
            spans.append([run_start, run_end])
            run_start = None

    merged: list[list[float]] = []
    for s in spans:
        if merged and s[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)

    clip_dur = end - start
    out: list[tuple[float, float]] = []
    for a, b in merged:
        if b - a < min_dur:
            continue
        out.append((round(max(0.0, a - pad), 3), round(min(clip_dur, b + pad), 3)))
    return out


def _merge_spans(spans: list[tuple[float, float]], gap: float = 0.04
                 ) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if b <= a:
            continue
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out]


def _complement(spans: list[tuple[float, float]], dur: float
                ) -> list[tuple[float, float]]:
    """Everything in [0, dur] NOT covered by `spans`."""
    out, prev = [], 0.0
    for a, b in _merge_spans(spans):
        if a > prev:
            out.append((prev, a))
        prev = max(prev, b)
    if prev < dur:
        out.append((prev, dur))
    return out


def resolve_spans(video: Path, start: float, end: float, cfg,
                  clip_dur: float) -> list[tuple[float, float]]:
    """Decide the spans (clip-local seconds) where the stack/wide overlay is shown.

    Priority:
    1. `[diarize].follow_director = true` -> show the wide view whenever the source
       is NOT on a single-person close-up, OR whenever 2+ people talk at once.
       (Punch into the reframe close-up only when the editor isolated one speaker.)
    2. `[diarize].gate = true` -> show the split ONLY while 2+ people talk at once.
    3. Always-on (`[stack].panels` / `.always`, `[triptych].always`) -> whole clip.
    4. Otherwise (triptych) -> detect the app-demo banner spans.
    """
    dcfg = cfg.raw.get("diarize", {})
    if dcfg.get("follow_director"):
        from . import diarize as _di
        from . import reframe as _rf
        duo = _di.duo_spans(_di.mode_timeline(video, start, end, cfg))
        closeup = _rf.detect_closeup_spans(video, start, end, cfg.raw.get("reframe", {}))
        return _merge_spans(_complement(closeup, clip_dur) + list(duo))
    if dcfg.get("gate"):
        from . import diarize as _di
        tl = _di.mode_timeline(video, start, end, cfg)
        return _di.duo_spans(tl)
    scfg = cfg.raw.get("stack", {})
    tcfg = cfg.raw.get("triptych", {})
    if scfg.get("panels") or scfg.get("always") or tcfg.get("always"):
        return [(0.0, round(clip_dur, 3))]
    return detect_phone_spans(video, start, end, cfg)


def _enable_expr(spans: list[tuple[float, float]], offset: float) -> str:
    terms = [f"between(t\\,{a + offset:.3f}\\,{b + offset:.3f})" for a, b in spans]
    return "+".join(terms) if terms else "0"


def overlay_tail(base_lbl: str, out_lbl: str, trip_in_lbl: str,
                 spans: list[tuple[float, float]], cfg, w: int, h: int,
                 offset: float = 0.0, pre: str = "") -> str:
    """Overlay the stack on a finished base video.

    `base_lbl` is the already-composited w x h base; `trip_in_lbl` is a raw source
    decode (its own ffmpeg input). The stack is built from that raw decode and
    overlaid on the base during `spans`. `pre` is an optional chain applied to the
    raw decode first (e.g. the freeze-intro `tpad`); `offset` shifts span times by
    the same amount. Produces `[out_lbl]`. Captions/title are layered AFTER this."""
    parts = []
    trip_src = trip_in_lbl
    if pre:
        parts.append(f"[{trip_in_lbl}]{pre}[trip_pre]")
        trip_src = "trip_pre"
    parts.append(stack_subgraph(trip_src, "trip_comp", cfg, w, h))
    parts.append(f"[{base_lbl}][trip_comp]"
                 f"overlay=0:0:enable='{_enable_expr(spans, offset)}'[{out_lbl}]")
    return ";".join(parts)
