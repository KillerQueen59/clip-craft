"""Face-aware auto-reframe: build a stable horizontal crop path that keeps the
speaker filling a vertical frame.

The camera in a multi-cam podcast already cuts to whoever is talking, so for the
common single-face shot we simply follow the on-screen face. Stability is the
whole game — we hold the crop still inside a shot (deadzone + slow glide) and only
snap on a hard cut. In the rarer two-shot (both people visible) we pick the
speaker by which mouth is moving, with a dwell timer so it can't ping-pong.

Output is a set of crop keyframes turned into an ffmpeg `crop` x-expression, so
the existing caption / title / hook pipeline burns on top unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("GLOG_minloglevel", "3")       # hush mediapipe/absl chatter
os.environ.setdefault("GRPC_VERBOSITY", "NONE")

import cv2  # noqa: E402


def geometry(src_w: int, src_h: int, out_w: int, out_h: int) -> dict:
    """Crop rectangle that matches the output aspect. For the usual 16:9->9:16
    case this is a full-height window that pans horizontally (only x varies)."""
    tar = out_w / out_h
    src_ar = src_w / src_h
    if src_ar > tar + 1e-3:                           # source wider -> pan x, full height
        cw = int(round(src_h * tar / 2) * 2)
        return {"cw": min(cw, src_w), "ch": src_h, "cy": 0, "axis": "x"}
    ch = int(round(src_w / tar / 2) * 2)              # source taller -> pan y (unused here)
    return {"cw": src_w, "ch": min(ch, src_h), "cy": max(0, (src_h - ch) // 2), "axis": "x"}


def _make_detector(src_w: int, src_h: int, conf: float):
    """YuNet face detector (OpenCV-native). Replaces the old BlazeFace short-range
    model, which missed small/distant faces and returned no landmarks. YuNet sees
    multiple + small faces and gives mouth-corner landmarks for a tight speaking
    ROI. Shared with asd.py so both reframe paths use one detector."""
    from . import asd as _asd
    return _asd._detector(src_w, src_h, conf)


def _faces(det, frame, min_face: float = 0.0):
    """Every face in `frame` as (cx, y, w, h, score, mx, my). (mx, my) is the
    mouth-corner midpoint from YuNet's landmarks. Faces narrower than
    min_face * frame-width are dropped (background / poster faces)."""
    det.setInputSize((frame.shape[1], frame.shape[0]))
    _, faces = det.detect(frame)
    if faces is None:
        return []
    fw = frame.shape[1]
    out = []
    for f in faces:
        x, y, w, h = float(f[0]), float(f[1]), float(f[2]), float(f[3])
        if w / fw < min_face:
            continue
        mx = (float(f[10]) + float(f[12])) / 2.0      # right + left mouth corner
        my = (float(f[11]) + float(f[13])) / 2.0
        out.append((x + w / 2, y, w, h, float(f[14]), mx, my))
    return out


def _mouth_energy(frame, mx, my, w, prev):
    """Motion in a face's mouth region vs the same face's previous ROI. The mouth
    centre (mx, my) comes from YuNet's landmarks, so the ROI stays tight even on a
    turned head. Returns (energy, roi) where roi is cached for the next compare."""
    r = max(6, int(w * 0.28))
    x0, x1 = int(max(0, mx - r)), int(min(frame.shape[1], mx + r))
    y0, y1 = int(max(0, my - r * 0.7)), int(min(frame.shape[0], my + r * 0.7))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return 0.0, prev
    roi = cv2.resize(cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), (32, 24))
    energy = 0.0 if prev is None else float(cv2.absdiff(roi, prev).mean())
    return energy, roi


def compute_crop_path(video: Path, start: float, end: float, out_w: int, out_h: int,
                      opts: dict | None = None) -> dict:
    """Analyze [start, end] and return {cw, ch, cy, kfs}. `kfs` is a list of
    (t_relative_to_start, x_left) crop keyframes."""
    o = opts or {}
    sample_fps = float(o.get("sample_fps", 6.0))
    conf = float(o.get("min_confidence", 0.5))
    ease = float(o.get("ease", 0.28))
    deadzone_frac = float(o.get("deadzone", 0.06))
    cut_diff = float(o.get("cut_diff", 16.0))
    dwell_s = float(o.get("switch_dwell", 1.0))       # min seconds on a face in a two-shot
    switch_margin = float(o.get("switch_margin", 1.4))  # speaker must beat listener by this
    min_face = float(o.get("min_face", 0.045))        # ignore faces below this frac of width

    cap = cv2.VideoCapture(str(video))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    geo = geometry(src_w, src_h, out_w, out_h)
    cw = geo["cw"]
    if geo["axis"] != "x" or cw >= src_w:             # nothing to pan (e.g. 16:9 output)
        cap.release()
        return {**geo, "src_w": src_w, "kfs": [(0.0, max(0, (src_w - cw) // 2))]}

    halfw = cw / 2
    lo, hi = halfw, src_w - halfw
    deadzone = deadzone_frac * cw
    cut_jump = 0.18 * src_w
    dwell = max(1, round(dwell_s * sample_fps))
    step = max(1, round(src_fps / sample_fps))

    det = _make_detector(src_w, src_h, conf)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(start * src_fps))
    fidx, end_f = int(start * src_fps), int(end * src_fps)

    kfs: list[tuple[float, int]] = []
    prev_small = None
    cur = None
    locked_cx = None
    last_face_cx = None                               # last frame's real face centre
    side = None                                       # 0 left / 1 right in a two-shot
    since_switch = 0
    engL = engR = 0.0
    roiL = roiR = None
    stats = {"single": 0, "two": 0, "none": 0}

    while fidx <= end_f:
        ok, frame = cap.read()
        if not ok:
            break
        t = fidx / src_fps
        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
        cut = prev_small is not None and \
            float(cv2.absdiff(small, prev_small).mean()) > cut_diff
        prev_small = small

        full = _faces(det, frame, min_face)
        big = [b for b in full if b[2] / src_w > 0.09]
        target = None
        switched = False                              # speaker changed -> snap, don't glide
        force = False                                 # no face -> snap onto the last real face

        if big:
            stats["single"] += 1
            if locked_cx is None or cut:
                target = max(big, key=lambda b: b[2] * b[3])[0]
            else:
                target = min(big, key=lambda b: abs(b[0] - locked_cx))[0]
                if abs(target - locked_cx) > cut_jump:
                    cut, target = True, max(big, key=lambda b: b[2] * b[3])[0]
            locked_cx = target
            side, roiL, roiR, engL, engR = None, None, None, 0.0, 0.0
        else:
            # YuNet returns every face at once, so split the full-frame detections
            # by the frame centre instead of re-detecting on left/right halves.
            L = [b for b in full if b[0] < 0.5 * src_w]
            R = [b for b in full if b[0] >= 0.5 * src_w]
            if L and R:                               # two-shot: pick the talking mouth
                stats["two"] += 1
                fl = max(L, key=lambda b: b[2] * b[3])
                fr = max(R, key=lambda b: b[2] * b[3])
                el, roiL = _mouth_energy(frame, fl[5], fl[6], fl[2], roiL)
                er, roiR = _mouth_energy(frame, fr[5], fr[6], fr[2], roiR)
                engL = 0.6 * engL + 0.4 * el
                engR = 0.6 * engR + 0.4 * er
                if side is None:                      # entering a two-shot: keep continuity
                    side = 0 if (cur is None or abs(fl[0] - cur) <= abs(fr[0] - cur)) else 1
                    since_switch = 0
                else:
                    since_switch += 1
                    want = 0 if engL >= engR else 1
                    strong = max(engL, engR) >= switch_margin * (min(engL, engR) + 1e-6)
                    if want != side and strong and since_switch >= dwell:
                        side, since_switch = want, 0
                        switched = True               # snap to the new talker, no void glide
                target = fl[0] if side == 0 else fr[0]
                locked_cx = target
            else:
                one = full or L or R
                if one:
                    stats["single"] += 1
                    target = min(one, key=lambda b: abs(b[0] - (locked_cx or src_w / 2)))[0]
                    locked_cx = target
                else:
                    # No face this frame: never drift onto empty space. Hold on the
                    # last real face (snap, not glide) so a pause keeps a person
                    # framed instead of stalling on the void.
                    stats["none"] += 1
                    target = last_face_cx
                    force = target is not None
                side, roiL, roiR = None, None, None

        if target is not None:
            last_face_cx = target

        if cur is None:
            cur = target if target is not None else src_w / 2
        elif target is not None and (cut or switched or force):
            cur = target                              # instant correction, never eased
        elif target is not None and abs(target - cur) > deadzone:
            cur += (target - cur) * ease
        cur = min(max(cur, lo), hi)
        x_left = int(round((cur - halfw) / 2) * 2)
        kfs.append((round(max(0.0, t - start), 3), max(0, min(src_w - cw, x_left))))

        for _ in range(step - 1):
            cap.grab(); fidx += 1
        fidx += 1
    cap.release()

    kfs = _rdp(kfs, float(o.get("simplify_px", 8.0))) if len(kfs) > 2 else kfs
    if not kfs:
        kfs = [(0.0, max(0, (src_w - cw) // 2))]
    return {**geo, "src_w": src_w, "kfs": kfs, "stats": stats}


def compute_speaker_crop_path(video: Path, start: float, end: float,
                              out_w: int, out_h: int, cfg,
                              opts: dict | None = None) -> dict:
    """Active-speaker focus cam. Diarization (audio-only, reliable) says WHO
    speaks WHEN; we learn each speaker's SEAT once — during a speaker's solo
    turns the face with the most mouth motion is them, so the median x of that
    face over all their turns pins them to a screen position — then crop to that
    seat while they speak. This never guesses per-frame: the seat is decided from
    the whole clip. A source close-up (one big face) is followed directly; duo
    spans are left flat for the wide pull-out overlay to cover.

    Same return shape as compute_crop_path: {cw, ch, cy, src_w, kfs, stats}."""
    import statistics

    o = opts or {}
    sample_fps = float(o.get("sample_fps", 6.0))
    conf = float(o.get("min_confidence", 0.5))
    ease = float(o.get("ease", 0.30))
    deadzone_frac = float(o.get("deadzone", 0.05))
    close_min = float(o.get("close_min", 0.28))
    match_px_frac = float(o.get("seat_match", 0.08))

    from . import diarize as _di
    tl = _di.mode_timeline(video, start, end, cfg)

    cap = cv2.VideoCapture(str(video))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    geo = geometry(src_w, src_h, out_w, out_h)
    cw = geo["cw"]
    if geo["axis"] != "x" or cw >= src_w:
        cap.release()
        return {**geo, "src_w": src_w, "kfs": [(0.0, max(0, (src_w - cw) // 2))]}

    match_px = match_px_frac * src_w
    step = max(1, round(src_fps / sample_fps))

    min_face = float(o.get("min_face", 0.045))
    det = _make_detector(src_w, src_h, conf)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(start * src_fps))
    fidx, end_f = int(start * src_fps), int(end * src_fps)

    # single pass: per sampled frame, every face with its mouth-motion energy
    samples: list[tuple[float, list[tuple[float, float, float]]]] = []
    prev_rois: list[tuple[float, object]] = []
    while fidx <= end_f:
        ok, frame = cap.read()
        if not ok:
            break
        t = fidx / src_fps - start
        info: list[tuple[float, float, float, float, float]] = []  # (cx, w, energy, y, h)
        cur_rois: list[tuple[float, object]] = []
        for (cx, y, w, h, _sc, mx, my) in _faces(det, frame, min_face):
            prev = None
            if prev_rois:                                  # match a face to itself across frames
                pc, pr = min(prev_rois, key=lambda p: abs(p[0] - cx))
                if abs(pc - cx) <= match_px:
                    prev = pr
            energy, roi = _mouth_energy(frame, mx, my, w, prev)
            cur_rois.append((cx, roi))
            info.append((cx, w, energy, y, h))
        prev_rois = cur_rois
        samples.append((t, info))
        for _ in range(step - 1):
            cap.grab(); fidx += 1
        fidx += 1
    cap.release()

    def entry_at(t: float) -> dict:
        for d in tl:
            if d["start"] <= t < d["end"]:
                return d
        return tl[-1] if tl else {"mode": "single", "speaker": None}

    # learn each speaker's seat from the loudest-mouth face during their solos.
    # A "solo" is often shot as a two-shot, so the candidate x's are bimodal
    # (the speaker AND the listener) — a plain median lands in the empty gap
    # between them. Instead cluster the candidates and take the dominant cluster
    # (weighted by mouth energy), so the seat snaps to the real speaking face.
    cand: dict[str, list[tuple[float, float]]] = {}
    for t, info in samples:
        if not info:
            continue
        d = entry_at(t)
        if d["mode"] != "single" or not d["speaker"]:
            continue
        cx, _w, e = max(info, key=lambda f: f[2])[:3]
        cand.setdefault(d["speaker"], []).append((cx, e))

    def _dominant_seat(points: list[tuple[float, float]]) -> float:
        gap = 0.05 * src_w
        clusters: list[list[tuple[float, float]]] = []
        for cx, e in sorted(points):
            if clusters and cx - clusters[-1][-1][0] <= gap:
                clusters[-1].append((cx, e))
            else:
                clusters.append([(cx, e)])
        best = max(clusters, key=lambda c: sum(e for _, e in c) + len(c) * 1e-6)
        wsum = sum(e for _, e in best) + 1e-6
        return sum(cx * e for cx, e in best) / wsum if wsum > 1e-6 else \
            statistics.median([cx for cx, _ in best])

    seat_x = {spk: _dominant_seat(v) for spk, v in cand.items() if v}

    # punch-in zoom: the full-height pan spans ~2 people on a wide couch, so a
    # focus cam needs a tighter 9:16 window. Shrink cw/ch by `zoom` and place it
    # vertically from where the speaking faces actually sit (headroom kept above).
    zoom = float(o.get("zoom", 1.0))
    if zoom > 1.01 and geo["axis"] == "x":
        tops, ctrs = [], []
        for t, info in samples:
            if not info:
                continue
            d = entry_at(t)
            if d["mode"] != "single" or not d["speaker"]:
                continue
            f = max(info, key=lambda f: f[2])
            tops.append(f[3]); ctrs.append(f[3] + f[4] / 2)
        cw = int(round(geo["cw"] / zoom / 2) * 2)
        ch = int(round(geo["ch"] / zoom / 2) * 2)
        if ctrs:
            face_ctr = statistics.median(ctrs)
            head_top = statistics.median(tops)
            cy = int(min(face_ctr - 0.45 * ch, head_top - 0.12 * ch))
        else:
            cy = (src_h - ch) // 2
        cy = max(0, min(src_h - ch, cy))
        geo = {**geo, "cw": cw, "ch": ch, "cy": cy}

    cw = geo["cw"]
    halfw = cw / 2
    lo, hi = halfw, src_w - halfw
    deadzone = deadzone_frac * cw

    kfs: list[tuple[float, int]] = []
    cur = None
    prev_spk: object = object()
    for t, info in samples:
        d = entry_at(t)
        spk = d["speaker"]
        big = [f for f in info if f[1] / src_w > close_min]
        if len(big) == 1:                                  # source cut to a close-up: follow it
            target = big[0][0]
        elif d["mode"] == "single" and spk in seat_x:      # on the master: go to their seat
            target = seat_x[spk]
        elif info:                                         # unknown framing: nearest face, no jump
            ref = cur if cur is not None else src_w / 2
            target = min(info, key=lambda f: abs(f[0] - ref))[0]
        else:
            target = None
        snap = spk != prev_spk                             # speaker changed -> cut, don't glide
        prev_spk = spk
        if target is None:
            if cur is None:
                cur = src_w / 2
        elif cur is None or snap:
            cur = target
        elif abs(target - cur) > deadzone:
            cur += (target - cur) * ease
        cur = min(max(cur, lo), hi)
        x_left = int(round((cur - halfw) / 2) * 2)
        kfs.append((round(max(0.0, t), 3), max(0, min(src_w - cw, x_left))))

    kfs = _rdp(kfs, float(o.get("simplify_px", 8.0))) if len(kfs) > 2 else kfs
    if not kfs:
        kfs = [(0.0, max(0, (src_w - cw) // 2))]
    seats = {spk: int(round(x)) for spk, x in sorted(seat_x.items())}
    print(f"[reframe] speaker seats (src px): {seats or 'none learned'}")
    return {**geo, "src_w": src_w, "kfs": kfs, "stats": {"seats": seats}}


def compute_asd_crop_path(video: Path, start: float, end: float,
                          out_w: int, out_h: int, cfg,
                          opts: dict | None = None) -> dict:
    """Focus cam driven by real audio-visual active-speaker detection. Unlike
    compute_speaker_crop_path (which learns one seat per diarized speaker), this
    reads clipcraft/asd.py's PER-FRAME speaking-face position — so it follows the
    talker even as the source cuts between the wide master and close-ups, and
    never lands on the gap between two people. Adds the same punch-in zoom so the
    talker fills the vertical frame. Same return shape as compute_crop_path."""
    o = opts or {}
    ease = float(o.get("ease", 0.30))
    deadzone_frac = float(o.get("deadzone", 0.05))
    zoom = float(o.get("zoom", 1.5))

    from . import asd as _asd
    tl = _asd.speaker_timeline(video, start, end, cfg)
    fps, n, speaking = tl["fps"], tl["n"], tl["speaking"]

    import cv2
    cap = cv2.VideoCapture(str(video))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    geo = geometry(src_w, src_h, out_w, out_h)
    if geo["axis"] != "x" or geo["cw"] >= src_w:
        return {**geo, "src_w": src_w, "kfs": [(0.0, max(0, (src_w - geo["cw"]) // 2))]}

    if zoom > 1.01:
        ctrs = [b[1] for b in speaking if b]
        tops = [b[1] - b[2] for b in speaking if b]      # box center y minus half-size
        cw = int(round(geo["cw"] / zoom / 2) * 2)
        ch = int(round(geo["ch"] / zoom / 2) * 2)
        if ctrs:
            import statistics
            cy = int(min(statistics.median(ctrs) - 0.42 * ch,
                         statistics.median(tops) - 0.10 * ch))
        else:
            cy = (src_h - ch) // 2
        geo = {**geo, "cw": cw, "ch": ch, "cy": max(0, min(src_h - ch, cy))}

    cw = geo["cw"]
    halfw = cw / 2
    lo, hi = halfw, src_w - halfw
    deadzone = deadzone_frac * cw
    cut_jump = 0.14 * src_w

    kfs: list[tuple[float, int]] = []
    cur = None
    spoke = 0
    for i in range(n):
        b = speaking[i]
        target = b[0] if b else None
        if target is not None:
            spoke += 1
            if cur is None or abs(target - cur) > cut_jump:   # new talker/shot -> cut
                cur = target
            elif abs(target - cur) > deadzone:
                cur += (target - cur) * ease
        elif cur is None:
            cur = src_w / 2                                    # nobody yet: center
        cur = min(max(cur, lo), hi)
        x_left = int(round((cur - halfw) / 2) * 2)
        kfs.append((round(i / fps, 3), max(0, min(src_w - cw, x_left))))

    kfs = _rdp(kfs, float(o.get("simplify_px", 8.0))) if len(kfs) > 2 else kfs
    if not kfs:
        kfs = [(0.0, max(0, (src_w - cw) // 2))]
    print(f"[reframe] ASD focus cam: {spoke}/{n} frames had an active speaker")
    return {**geo, "src_w": src_w, "kfs": kfs, "stats": {"asd_frames": spoke}}


def detect_closeup_spans(video: Path, start: float, end: float,
                         opts: dict | None = None) -> list[tuple[float, float]]:
    """Spans (clip-local seconds) where the SOURCE is on a single-person close-up
    (a face wider than `close_min` of the frame). Used by the "follow the director"
    layout: punch into the reframe crop during these, show the wide view otherwise.
    Runs > the wide-master and B&W-inset framings, where no face is that large, are
    left out on purpose so they fall back to wide."""
    o = opts or {}
    sample_fps = float(o.get("sample_fps", 6.0))
    conf = float(o.get("min_confidence", 0.5))
    close_min = float(o.get("close_min", 0.22))
    min_dur = float(o.get("closeup_min_seconds", 0.6))
    merge_gap = float(o.get("closeup_merge_gap", 0.5))
    pad = float(o.get("closeup_pad", 0.10))

    cap = cv2.VideoCapture(str(video))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(src_fps / sample_fps))
    det = _make_detector(src_w, src_h, conf)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(start * src_fps))
    fidx, end_f = int(start * src_fps), int(end * src_fps)
    dt = step / src_fps

    samples: list[tuple[float, bool]] = []
    while fidx <= end_f:
        ok, frame = cap.read()
        if not ok:
            break
        boxes = _faces(det, frame)
        is_close = any(b[2] / src_w > close_min for b in boxes)
        samples.append((fidx / src_fps - start, is_close))
        for _ in range(step - 1):
            cap.grab(); fidx += 1
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


def apply_locks(kfs: list[tuple[float, float]], locks: list[dict],
                geo: dict) -> list[tuple[float, float]]:
    """Override the auto-tracked crop with a fixed horizontal hold over given
    time windows (body-relative seconds). Each lock is a dict with `t0`, `t1`,
    and either `x` (crop-left px) or `cx` (face-center px). The path glides into
    and out of the held position from the surrounding auto keyframes."""
    cw = geo["cw"]
    xmax = max(0, int(geo.get("src_w", cw)) - cw)

    def _x(l: dict) -> int:
        x = float(l["x"]) if "x" in l else float(l["cx"]) - cw / 2.0
        return int(round(min(max(x, 0.0), xmax) / 2) * 2)

    pts = list(kfs)
    for l in locks:
        t0, t1, x = float(l["t0"]), float(l["t1"]), _x(l)
        pts = [(t, xx) for (t, xx) in pts if not (t0 <= t <= t1)]
        pts.append((t0, x))
        pts.append((t1, x))
    pts.sort(key=lambda p: p[0])
    return pts


def _rdp(points, eps):
    """Ramer-Douglas-Peucker on a (t, x) polyline: keep corners, drop straights."""
    if len(points) < 3:
        return points
    x0, y0 = points[0]; x1, y1 = points[-1]
    dmax, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        x, y = points[i]
        d = abs(y - y0) if x1 == x0 else abs(y - (y0 + (y1 - y0) * (x - x0) / (x1 - x0)))
        if d > dmax:
            dmax, idx = d, i
    if dmax > eps:
        return _rdp(points[:idx + 1], eps)[:-1] + _rdp(points[idx:], eps)
    return [points[0], points[-1]]


def _expr_from_kfs(kfs: list[tuple[float, float]]) -> str:
    """Piecewise-linear x(t) as a FLAT sum of interval-gated ramps. A flat sum
    (not nested if()) avoids ffmpeg's expression recursion-depth limit, which a
    nested form blows past on clips with many keyframes (e.g. two-shot switching).
    Exactly one interval is active at any t, so the terms sum to the right value.
    Commas are escaped for use inside filter_complex."""
    terms, first = [], True
    for i in range(len(kfs) - 1):
        t0, x0 = kfs[i]; t1, x1 = kfs[i + 1]
        if t1 <= t0:                                   # zero-width (a hard cut step): skip
            continue
        slope = (x1 - x0) / (t1 - t0)
        cond = f"lt(t\\,{t1:.4f})" if first else f"(gte(t\\,{t0:.4f})*lt(t\\,{t1:.4f}))"
        terms.append(f"{cond}*({int(x0)}+({slope:.3f})*(t-{t0:.4f}))")
        first = False
    tl, xl = kfs[-1]
    terms.append(f"gte(t\\,{tl:.4f})*{int(xl)}")       # hold last value to the end
    return "+".join(terms)


_MAX_KFS = 45   # ffmpeg's expression parser fails past ~80-100 terms; stay well under


def _cap(kfs, max_n=_MAX_KFS):
    """Thin the keyframes until they fit in one ffmpeg expression, raising the RDP
    tolerance until the count is under the cap. Big corners (speaker switches, hard
    cuts) survive; small intra-shot drift is dropped."""
    if len(kfs) <= max_n:
        return kfs
    eps, out = 8.0, kfs
    while len(out) > max_n and eps < 500:
        eps *= 1.5
        out = _rdp(kfs, eps)
    return out


def crop_filter(geo: dict, segments: list[tuple[list, float]], out_w: int,
                out_h: int) -> str:
    """Base video filter for fit=reframe. `segments` is the ordered list of
    (kfs, duration) that ffmpeg concatenates BEFORE this filter runs — one entry
    for the plain body, or [hook, body] for a cold open. Their crop paths are
    stitched onto one output timeline so x tracks correctly across the join."""
    merged: list[tuple[float, float]] = []
    off = 0.0
    for kfs, dur in segments:
        for t, x in kfs:
            tt = off + min(t, dur)
            if merged and tt <= merged[-1][0]:
                tt = merged[-1][0] + 1e-3
            merged.append((tt, x))
        off += dur
    expr = _expr_from_kfs(_cap(merged))
    return (f"crop={geo['cw']}:{geo['ch']}:x='{expr}':y={geo['cy']},"
            f"scale={out_w}:{out_h},setsar=1")
