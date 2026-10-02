"""Dynamic duo-split base layer.

When two people talk at once (diarized overlap) AND the source shows both faces
(wide master / 2-shot), render a top/bottom split of the two TALKING faces;
otherwise fall back to the normal YuNet single-crop reframe. The layout changes
per shot, so the clip is built as a sequence of segments (split or single) that
are rendered independently and concatenated. Face detection + the single-crop
path are shared with `reframe.py` (YuNet); diarization gates *when* to split,
vision (mouth motion) picks *who*.

`build_base()` returns a plain vertical video (no captions/title/outro); the
render pipeline finishes it via `render_finish` so captions/title/watermark/outro
burn on top, exactly like the raw-cut → finish workflow.

Why a separate base file instead of one filter graph: a time-varying layout
(single ↔ two-panel) can't be a single ffmpeg crop expression, so we concat.
"""
from __future__ import annotations

import statistics
import subprocess
import tempfile
from pathlib import Path

import cv2

from . import reframe as _rf


def _opts(cfg):
    o = dict(cfg.raw.get("reframe", {}))
    o.update(cfg.raw.get("duosplit", {}))          # duosplit.* overrides reframe.*
    return o


def _overlap_gate(video: Path, start: float, end: float, cfg):
    """Return a predicate in_duo(t_clip_local) from diarization. Degrades to
    'never overlap' (all single reframe) if diarization is unavailable."""
    try:
        from . import diarize as _di
        d = dict(cfg.raw.get("diarize", {}))
        d.setdefault("enabled", True)
        cfg.raw["diarize"] = d
        tl = _di.mode_timeline(video, start, end, cfg)
        duo = [(x["start"], x["end"]) for x in tl if x["mode"] == "duo"]

        def in_duo(t):
            return any(a <= t < b for a, b in duo)
        return in_duo, sum(b - a for a, b in duo)
    except Exception as exc:                        # no token / pyannote → plain reframe
        print(f"[duosplit] diarization unavailable ({exc}); single-crop only.")
        return (lambda t: False), 0.0


def _pick_two(video, a, b, W, H, FPS, det, conf_opts):
    """Top-2 talkers by mouth motion over source-time [a,b]; each face is
    (cx,cy,w,h). Returns 2 faces or None. A motionless face (wall photo) can never
    be chosen — a real talker's mouth moves."""
    sample_fps = float(conf_opts.get("sample_fps", 8.0))
    min_face = float(conf_opts.get("min_face", 0.02))
    e_floor = float(conf_opts.get("split_energy_floor", 1.5))
    c = cv2.VideoCapture(str(video))
    c.set(cv2.CAP_PROP_POS_FRAMES, int(a * FPS))
    fi, ef = int(a * FPS), int(b * FPS)
    st = max(1, round(FPS / sample_fps))
    tracks: dict = {}
    prev: dict = {}
    while fi <= ef:
        ok, frame = c.read()
        if not ok:
            break
        if (fi - int(a * FPS)) % st == 0:
            cur = {}
            for (cx, y, w, h, sc, mx, my) in _rf._faces(det, frame, min_face):
                key = min(tracks, key=lambda k: abs(k - cx), default=None)
                if key is None or abs(key - cx) > 0.06 * W:
                    key = cx
                    tracks[key] = {"xs": [], "ys": [], "ws": [], "hs": [], "e": 0.0}
                em, roi = _rf._mouth_energy(frame, mx, my, w, prev.get(key))
                cur[key] = roi
                tr = tracks[key]
                tr["xs"].append(cx); tr["ys"].append(y + h / 2)
                tr["ws"].append(w); tr["hs"].append(h); tr["e"] += em
            prev = cur
        fi += 1
    c.release()
    good = [t for t in tracks.values()
            if len(t["xs"]) >= 3 and t["e"] / len(t["xs"]) >= e_floor]
    if len(good) < 2:
        return None
    two = sorted(good, key=lambda t: t["e"], reverse=True)[:2]
    two = sorted(two, key=lambda t: statistics.median(t["xs"]))
    faces = [(statistics.median(t["xs"]), statistics.median(t["ys"]),
              statistics.median(t["ws"]), statistics.median(t["hs"])) for t in two]
    (cx0, _, w0, _), (cx1, _, w1, _) = faces
    # Two tracks whose boxes horizontally overlap are the SAME person (a
    # double-detection or a split track) — splitting them shows the same face in
    # both panels. Only split when the faces are genuinely apart.
    if abs(cx1 - cx0) < 1.1 * max(w0, w1):
        return None
    return faces


def _panel_crop(cx, cy, w, h, par, W, H):
    ch = min(max(int(h * 3.2), 300), H)
    cw = int(ch * par)
    if cw > W:
        cw = W; ch = min(int(cw / par), H)
    cw -= cw % 2; ch -= ch % 2
    x = max(0, min(W - cw, int(cx - cw / 2)))
    y = max(0, min(H - ch, int(cy - ch * 0.42)))
    return cw, ch, x - x % 2, y - y % 2


def _force_single_intervals(clip: dict | None, dur: float) -> list[tuple[float, float]]:
    """Manual overrides (clip-local seconds) that force the single crop instead of
    the auto split: `duosplit_single_after` (that second → end) and each span in
    `duosplit_single_windows`. Out-of-range/degenerate values are clamped/dropped."""
    if not clip:
        return []
    ivs: list[tuple[float, float]] = []
    after = clip.get("duosplit_single_after")
    if after is not None:
        a = max(0.0, min(dur, float(after)))
        if a < dur:
            ivs.append((a, dur))
    for win in clip.get("duosplit_single_windows") or []:
        try:
            a, b = max(0.0, float(win[0])), min(dur, float(win[1]))
        except (TypeError, ValueError, IndexError):
            continue
        if b - a > 0.01:
            ivs.append((a, b))
    return ivs


def _apply_single_override(segs: list[list], dur: float,
                           ivs: list[tuple[float, float]]) -> list[list]:
    """Subdivide `segs` at every override edge, then force any sub-segment whose
    midpoint falls inside a force-single interval to mode 'single'. Cuts land
    exactly on the requested seconds; segments outside the intervals are untouched."""
    if not ivs:
        return segs
    edges = sorted({e for iv in ivs for e in iv})
    out: list[list] = []
    for mode, a, b in segs:
        pts = [a] + [e for e in edges if a < e < b] + [b]
        for j in range(len(pts) - 1):
            sa, sb = pts[j], pts[j + 1]
            mid = (sa + sb) / 2
            m = "single" if any(x <= mid < y for x, y in ivs) else mode
            out.append([m, sa, sb])
    merged: list[list] = []
    for r in out:
        if merged and merged[-1][0] == r[0]:
            merged[-1][2] = r[2]
        else:
            merged.append(list(r))
    return merged


def build_base(video: Path, start: float, end: float, w: int, h: int, cfg,
               opts: dict | None = None, clip: dict | None = None) -> Path:
    """Render the dynamic duo-split base for source window [start, end] to a
    temp mp4 (clip-local, w x h, with audio) and return its path.

    Per-clip manual overrides (from the manifest `clip` entry) can force the
    single crop over parts of the timeline — see `_force_single_intervals`."""
    o = opts or _opts(cfg)
    ff = cfg.ffmpeg or "ffmpeg"
    sample_fps = float(o.get("sample_fps", 8.0))
    min_face = float(o.get("min_face", 0.02))
    conf = float(o.get("min_confidence", 0.5))
    min_seg = float(o.get("min_seg", 1.2))
    cut_diff = float(o.get("cut_diff", 16.0))

    cap = cv2.VideoCapture(str(video))
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    FPS = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()
    dur = round(end - start, 3)

    in_duo, duo_tot = _overlap_gate(video, start, end, cfg)
    det = _rf._make_detector(W, H, conf)

    # pass 1: split-eligibility + shot-cuts, sampled over the window
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(start * FPS))
    step = max(1, round(FPS / sample_fps))
    fidx, end_f = int(start * FPS), int(end * FPS)
    marks: list[tuple[float, bool]] = []
    cuts: list[float] = []
    prev_small = None
    i = 0
    while fidx <= end_f:
        ok, frame = cap.read()
        if not ok:
            break
        if i % step == 0:
            t = fidx / FPS - start
            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
            if prev_small is not None and \
                    float(cv2.absdiff(small, prev_small).mean()) > cut_diff:
                cuts.append(t)
            prev_small = small
            n = len(_rf._faces(det, frame, min_face))
            marks.append((t, in_duo(t) and n >= 2))
        fidx += 1
        i += 1
    cap.release()

    # runs + dwell hysteresis
    runs: list[list] = []
    for t, sp in marks:
        mode = "split" if sp else "single"
        if runs and runs[-1][0] == mode:
            runs[-1][2] = t
        else:
            runs.append([mode, t, t])
    if not runs:
        runs = [["single", 0.0, dur]]
    runs[0][1] = 0.0; runs[-1][2] = dur
    for k in range(len(runs) - 1):
        runs[k][2] = runs[k + 1][1]
    for k, r in enumerate(runs):
        if r[2] - r[1] < min_seg and k > 0:
            r[0] = runs[k - 1][0]
    merged: list[list] = []
    for r in runs:
        if merged and merged[-1][0] == r[0]:
            merged[-1][2] = r[2]
        else:
            merged.append(list(r))
    # subdivide split segments at shot cuts (fixed crop is per-shot only)
    sub: list[list] = []
    for mode, a, b in merged:
        if mode != "split":
            sub.append([mode, a, b]); continue
        bounds = [a] + [c for c in cuts if a < c < b] + [b]
        for j in range(len(bounds) - 1):
            sub.append(["split", bounds[j], bounds[j + 1]])
    # absorb micro-shots (<0.4s) into the previous segment (keep timeline whole)
    segs: list[list] = []
    for s in sub:
        if s[2] - s[1] < 0.4 and segs:
            segs[-1][2] = s[2]
        else:
            segs.append(list(s))

    forced = _force_single_intervals(clip, dur)
    if forced:
        segs = _apply_single_override(segs, dur, forced)
        spans = ", ".join(f"{a:.1f}-{b:.1f}s" for a, b in forced)
        print(f"[duosplit] manual single-crop override: {spans}")

    n_split = sum(1 for m in segs if m[0] == "split")
    print(f"[duosplit] {video.name} {start:.1f}-{end:.1f}s: {len(segs)} segments "
          f"({n_split} split), {duo_tot:.1f}s overlap, {len(cuts)} cuts")

    tmp = Path(tempfile.mkdtemp(prefix="duosplit_"))
    par = w / (h / 2)
    seglist: list[Path] = []
    for idx, (mode, a, b) in enumerate(segs):
        d = b - a
        if d < 0.15:
            continue
        seg = tmp / f"seg_{idx:03d}.mp4"
        if mode == "split":
            two = _pick_two(video, start + a, start + b, W, H, FPS, det, o)
            if two is None:
                mode = "single"
        if mode == "split":
            (cwa, cha, xa, ya) = _panel_crop(*two[0], par, W, H)
            (cwb, chb, xb, yb) = _panel_crop(*two[1], par, W, H)
            vf = (f"[0:v]crop={cwa}:{cha}:{xa}:{ya},scale={w}:{h // 2}[t];"
                  f"[0:v]crop={cwb}:{chb}:{xb}:{yb},scale={w}:{h // 2}[b];"
                  f"[t][b]vstack[v]")
        else:
            body = _rf.compute_crop_path(video, start + a, start + b, w, h,
                                         {"min_confidence": conf, "min_face": min_face})
            crop = _rf.crop_filter(body, [(body["kfs"], d)], w, h)
            vf = f"[0:v]{crop}[v]"
        cmd = [ff, "-y", "-v", "error", "-ss", f"{start + a:.3f}", "-i", str(video),
               "-t", f"{d:.3f}", "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
               "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
               "-c:a", "aac", "-ar", "48000", "-video_track_timescale", "30000", str(seg)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[duosplit] seg {idx} ({mode}) failed: {r.stderr[-300:]}")
            continue
        seglist.append(seg)

    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{s.as_posix()}'\n" for s in seglist), encoding="utf-8")
    base = tmp / "base.mp4"
    cc = [ff, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
          "-c:a", "aac", str(base)]
    r = subprocess.run(cc, capture_output=True, text=True)
    if r.returncode != 0 or not base.exists():
        raise RuntimeError(f"duosplit concat failed: {r.stderr[-500:]}")
    return base
