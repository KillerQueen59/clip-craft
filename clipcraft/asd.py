"""Active-speaker reframe for multi-person / non-static multicam.

The plain `reframe.py` follows one face and, in a two-shot, picks the talking
mouth by per-frame motion. It breaks when there are >2 people, when faces are
small/distant (the blazeface short-range model misses them), or when the source
cuts between camera angles — because it never knows *which* of many faces is the
speaker.

This module fixes WHO-is-talking by anchoring vision to audio:

  1. DETECT every face per sampled frame with YuNet (OpenCV-native, sees small
     and multiple faces, and returns mouth-corner landmarks).
  2. TRACK faces into stable tracks (IoU), broken on shot cuts so a track never
     survives a camera change.
  3. ANCHOR to diarization: pyannote (audio-only, robust) says WHEN exactly one
     person talks and WHO. Within each single-speaker span the on-screen speaker
     is the track whose mouth moves most — evaluated over the *whole span*, via a
     stable track, so a static poster face or a silent listener can't win. Each
     speaker's screen SEAT is the median position of their active track across the
     clip, so wide/ambiguous framings fall back to a learned position instead of
     guessing.

Output is the same {cw, ch, cy, src_w, kfs} crop body the existing
`reframe.crop_filter` consumes, so captions / titles / hook / outro burn on top
unchanged. Duo (overlap) spans currently frame between the two speakers' seats;
the dynamic two-panel stack for those spans is the next step (see
docs/stack-layouts.md).

Standalone check:
  py -3 -m clipcraft.asd <video> <start> <end> [config.toml]
"""
from __future__ import annotations

import os
import statistics
import sys
from pathlib import Path

os.environ.setdefault("GLOG_minloglevel", "3")

import cv2  # noqa: E402

from .reframe import _rdp, geometry  # noqa: E402

_YUNET = Path(__file__).parent / "models" / "face_detection_yunet_2023mar.onnx"


def _detector(src_w: int, src_h: int, conf: float):
    if not _YUNET.exists():
        raise RuntimeError(
            f"YuNet model missing: {_YUNET}. Download "
            "face_detection_yunet_2023mar.onnx from the OpenCV Zoo into "
            "clipcraft/models/.")
    return cv2.FaceDetectorYN_create(
        str(_YUNET), "", (src_w, src_h),
        score_threshold=conf, nms_threshold=0.3, top_k=64)


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = max(0.0, x1 - x0), max(0.0, y1 - y0)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _mouth_energy(frame, mx, my, w, prev):
    """Motion in a face's mouth region vs the same track's previous ROI. The
    mouth centre comes from YuNet's mouth-corner landmarks, so the ROI is tight
    even on a turned head. Returns (energy, roi)."""
    r = max(6, int(w * 0.28))
    x0, x1 = int(max(0, mx - r)), int(min(frame.shape[1], mx + r))
    y0, y1 = int(max(0, my - r * 0.7)), int(min(frame.shape[0], my + r * 0.7))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return 0.0, prev
    roi = cv2.resize(cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), (32, 24))
    energy = 0.0 if prev is None else float(cv2.absdiff(roi, prev).mean())
    return energy, roi


class _Track:
    __slots__ = ("id", "box", "last", "pts", "energy", "roi")

    def __init__(self, tid, box, fidx):
        self.id = tid
        self.box = box            # last (x, y, w, h)
        self.last = fidx          # last sample index seen
        self.pts = []             # (t, cx, cy, w, h)
        self.energy = []          # (t, mouth_energy)
        self.roi = None           # cached mouth ROI for motion diff


def analyze(video: Path, start: float, end: float, opts: dict | None = None) -> dict:
    """Detect + track every face across [start, end] and measure each track's
    mouth motion. Returns {src_w, src_h, src_fps, tracks, shots, times}."""
    o = opts or {}
    sample_fps = float(o.get("sample_fps", 8.0))
    conf = float(o.get("min_confidence", 0.6))
    min_face = float(o.get("min_face", 0.045))     # ignore faces smaller than this frac of width
    cut_diff = float(o.get("cut_diff", 16.0))
    iou_thr = float(o.get("iou", 0.25))
    max_gap = int(o.get("track_gap", 2))           # bridge this many missed samples

    cap = cv2.VideoCapture(str(video))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    det = _detector(src_w, src_h, conf)

    step = max(1, round(src_fps / sample_fps))
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(start * src_fps))
    fidx, end_f = int(start * src_fps), int(end * src_fps)

    tracks: list[_Track] = []
    active: list[_Track] = []
    shots: list[float] = []                        # clip-local times of shot cuts
    times: list[float] = []
    prev_small = None
    next_id = 0
    si = 0                                          # sample index

    while fidx <= end_f:
        ok, frame = cap.read()
        if not ok:
            break
        t = round(fidx / src_fps - start, 3)
        times.append(t)

        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
        cut = prev_small is not None and \
            float(cv2.absdiff(small, prev_small).mean()) > cut_diff
        prev_small = small
        if cut:
            shots.append(t)
            active = []                            # a camera change ends every track

        det.setInputSize((src_w, src_h))
        _, faces = det.detect(frame)
        dets = []
        if faces is not None:
            for f in faces:
                x, y, w, h = float(f[0]), float(f[1]), float(f[2]), float(f[3])
                if w / src_w < min_face:
                    continue
                mx = (float(f[10]) + float(f[12])) / 2.0   # mouth-corner midpoint
                my = (float(f[11]) + float(f[13])) / 2.0
                dets.append((x, y, w, h, mx, my))

        used = set()
        for tr in active:
            if si - tr.last > max_gap:
                continue
            best, bj = iou_thr, -1
            for j, d in enumerate(dets):
                if j in used:
                    continue
                v = _iou(tr.box, d[:4])
                if v > best:
                    best, bj = v, j
            if bj >= 0:
                d = dets[bj]
                used.add(bj)
                tr.box = d[:4]
                tr.last = si
                cx, cy = d[0] + d[2] / 2, d[1] + d[3] / 2
                e, tr.roi = _mouth_energy(frame, d[4], d[5], d[2], tr.roi)
                tr.pts.append((t, cx, cy, d[2], d[3]))
                tr.energy.append((t, e))
        for j, d in enumerate(dets):
            if j in used:
                continue
            tr = _Track(next_id, d[:4], si)
            next_id += 1
            cx, cy = d[0] + d[2] / 2, d[1] + d[3] / 2
            e, tr.roi = _mouth_energy(frame, d[4], d[5], d[2], None)
            tr.pts.append((t, cx, cy, d[2], d[3]))
            tr.energy.append((t, e))
            tracks.append(tr)
            active.append(tr)

        for _ in range(step - 1):
            cap.grab(); fidx += 1
        fidx += 1
        si += 1
    cap.release()

    return {"src_w": src_w, "src_h": src_h, "src_fps": src_fps,
            "tracks": tracks, "shots": shots, "times": times}


def _speaker_at(tl: list[dict], t: float) -> tuple[str, object]:
    for d in tl:
        if d["start"] <= t < d["end"]:
            return d["mode"], d["speaker"]
    return ("single", None) if not tl else (tl[-1]["mode"], tl[-1]["speaker"])


def _energy_in(tr: _Track, a: float, b: float) -> tuple[float, int]:
    tot, n = 0.0, 0
    for t, e in tr.energy:
        if a <= t < b:
            tot += e; n += 1
    return tot, n


def layout(video: Path, start: float, end: float, cfg, opts: dict | None = None) -> dict:
    """Assign the audio speaker to a face track per single-speaker span, learn
    each speaker's seat, and return everything needed to build a crop path:
    {meta, tl, seats, active_by_span, samples_target}."""
    o = opts or {}
    an = analyze(video, start, end, o)
    tracks = an["tracks"]

    from . import diarize as _di
    tl = _di.mode_timeline(video, start, end, cfg)

    # single-speaker spans, in order
    spans = [(d["start"], d["end"], d["speaker"]) for d in tl if d["mode"] == "single"]
    active_by_span: dict[tuple[float, float], int] = {}
    seat_samples: dict[str, list[float]] = {}
    face_tops: list[float] = []
    face_ctrs: list[float] = []
    for a, b, spk in spans:
        best_tr, best_e = None, -1.0
        for tr in tracks:
            tot, n = _energy_in(tr, a, b)
            if n == 0:
                continue
            # per-sample mean mouth motion over the span; the talker moves most
            if tot / n > best_e:
                best_e, best_tr = tot / n, tr
        if best_tr is None:
            continue
        active_by_span[(a, b)] = best_tr.id
        for t, cx, cy, w, h in best_tr.pts:
            if a <= t < b:
                if spk:
                    seat_samples.setdefault(spk, []).append(cx)
                face_tops.append(cy - h / 2)
                face_ctrs.append(cy)
    seats = {spk: statistics.median(v) for spk, v in seat_samples.items() if v}

    return {"meta": an, "tl": tl, "tracks": {tr.id: tr for tr in tracks},
            "seats": seats, "active_by_span": active_by_span,
            "face_tops": face_tops, "face_ctrs": face_ctrs}


def compute_asd_crop_path(video: Path, start: float, end: float,
                          out_w: int, out_h: int, cfg,
                          opts: dict | None = None) -> dict:
    """Active-speaker crop path. Same return shape as
    reframe.compute_speaker_crop_path: {cw, ch, cy, src_w, kfs, stats}."""
    o = opts or {}
    ease = float(o.get("ease", 0.30))
    deadzone_frac = float(o.get("deadzone", 0.05))
    zoom = float(o.get("zoom", 1.0))

    lay = layout(video, start, end, cfg, o)
    an = lay["meta"]
    src_w, src_h = an["src_w"], an["src_h"]
    tracks, tl = lay["tracks"], lay["tl"]
    seats, active_by_span = lay["seats"], lay["active_by_span"]

    geo = geometry(src_w, src_h, out_w, out_h)
    if geo["axis"] != "x" or geo["cw"] >= src_w:
        return {**geo, "src_w": src_w, "kfs": [(0.0, max(0, (src_w - geo["cw"]) // 2))],
                "stats": {"seats": {}}}

    # punch-in: shrink the full-height window and place it over the faces
    if zoom > 1.01:
        cw = int(round(geo["cw"] / zoom / 2) * 2)
        ch = int(round(geo["ch"] / zoom / 2) * 2)
        if lay["face_ctrs"]:
            face_ctr = statistics.median(lay["face_ctrs"])
            head_top = statistics.median(lay["face_tops"])
            cy = int(min(face_ctr - 0.45 * ch, head_top - 0.12 * ch))
        else:
            cy = (src_h - ch) // 2
        geo = {**geo, "cw": cw, "ch": ch, "cy": max(0, min(src_h - ch, cy))}

    cw = geo["cw"]
    halfw = cw / 2
    lo, hi = halfw, src_w - halfw
    deadzone = deadzone_frac * cw

    def track_cx_at(tid: int, t: float) -> float | None:
        tr = tracks.get(tid)
        if not tr:
            return None
        best, bt = None, 1e9
        for tt, cx, cy, w, h in tr.pts:
            if abs(tt - t) < bt:
                bt, best = abs(tt - t), cx
        return best if bt <= 0.5 else None

    def span_of(t):
        for (a, b) in active_by_span:
            if a <= t < b:
                return (a, b)
        return None

    def faces_cx_at(t: float) -> list[float]:
        """Centres of every face actually detected near t (this camera angle)."""
        out = []
        for tr in tracks.values():
            best, bt = None, 1e9
            for tt, cx, cy, w, h in tr.pts:
                d = abs(tt - t)
                if d < bt:
                    bt, best = d, cx
            if best is not None and bt <= 0.5:
                out.append(best)
        return out

    kfs: list[tuple[float, int]] = []
    cur = None
    prev_key: object = object()
    for t in an["times"]:
        mode, spk = _speaker_at(tl, t)
        if mode == "duo":
            # frame between the two most-recent speakers' seats (graceful; a true
            # two-panel stack for duo spans is the next step)
            ss = sorted(seats.values())
            target = sum(ss) / len(ss) if ss else (cur if cur is not None else src_w / 2)
            key = "duo"
        else:
            sp = span_of(t)
            target = None
            if sp and sp in active_by_span:
                target = track_cx_at(active_by_span[sp], t)
            if target is None and spk in seats:
                target = seats[spk]
            if target is None:
                target = cur if cur is not None else src_w / 2
            key = spk or "single"

        # Never frame empty space. Seats are a clip-wide median, so on switched
        # multicam (or a duo midpoint between two seats) the target can land where
        # nobody is on THIS camera angle. If no detected face falls inside the crop,
        # snap to the nearest real face — showing the person on screen beats a blank
        # wall, and the snap is immediate so we don't ease through the void.
        force_snap = False
        faces = faces_cx_at(t)
        if faces and not any(abs(f - target) <= 0.85 * halfw for f in faces):
            target = min(faces, key=lambda f: abs(f - target))
            force_snap = True

        snap = key != prev_key
        prev_key = key
        if cur is None or snap or force_snap:
            cur = target
        elif abs(target - cur) > deadzone:
            cur += (target - cur) * ease
        cur = min(max(cur, lo), hi)
        x_left = int(round((cur - halfw) / 2) * 2)
        kfs.append((t, max(0, min(src_w - cw, x_left))))

    kfs = _rdp(kfs, float(o.get("simplify_px", 8.0))) if len(kfs) > 2 else kfs
    if not kfs:
        kfs = [(0.0, max(0, (src_w - cw) // 2))]
    seat_px = {spk: int(round(x)) for spk, x in sorted(seats.items())}
    n_tracks = len(tracks)
    print(f"[asd] {n_tracks} track(s), {len(an['shots'])} shot-cut(s); "
          f"seats(src px): {seat_px or 'none'}")
    return {**geo, "src_w": src_w, "kfs": kfs,
            "stats": {"seats": seat_px, "tracks": n_tracks, "shots": len(an["shots"])}}


def _main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: py -3 -m clipcraft.asd <video> <start> <end> [config.toml]")
        return 2
    from . import config as _cfg
    video = Path(argv[0])
    start, end = float(argv[1]), float(argv[2])
    cfg = _cfg.load(Path(argv[3]) if len(argv) > 3 else None)
    o = cfg.raw.get("reframe", {})
    lay = layout(video, start, end, cfg, o)
    print(f"clip {start:.2f}-{end:.2f}s  ({end - start:.1f}s)")
    print(f"tracks: {len(lay['tracks'])}  shots: {len(lay['meta']['shots'])}")
    print(f"seats (src px): {{" +
          ", ".join(f'{k}: {int(v)}' for k, v in sorted(lay['seats'].items())) + "}")
    print("timeline:")
    for d in lay["tl"]:
        tid = ""
        if d["mode"] == "single":
            for (a, b), t in lay["active_by_span"].items():
                if abs(a - d["start"]) < 1e-6 and abs(b - d["end"]) < 1e-6:
                    tid = f"  -> track {t}"
        print(f"  {d['start']:6.2f} - {d['end']:6.2f}  {d['mode']:6}  "
              f"{d['speaker'] or 'BOTH'}{tid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
