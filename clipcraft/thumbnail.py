"""Auto-thumbnail: pick the frame where the person is clearest (largest, in-focus
face) within a clip, then burn the title over it.

Face detection selects a frame that actually shows the creator rather than a
full-screen chart. Falls back to the sharpest sampled frame if no face is found.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Config
from .render import _ass_color, _esc, _wrap, _fmt_ass_time


def _best_frame(video: Path, start: float, end: float,
                samples: int = 25) -> tuple[float, float | None, int, int]:
    """Pick the frame that best shows a PERSON in [start, end].

    Returns (timestamp, face_cx_fraction | None, src_w, src_h). A thumbnail must
    contain a person, so we detect faces with YuNet (the same detector the reframe
    path uses — the old Haar cascade is gone in OpenCV 5) and choose the frame with
    the largest, most confident face. `face_cx_fraction` is that face's horizontal
    centre (0-1) so a vertical (9:16) crop can be positioned ON the person instead
    of centre-cropping them out of frame. Falls back to the sharpest frame only
    when NO sample contains a detectable face."""
    try:
        import cv2
    except ImportError:
        return (start + end) / 2.0, None, 0, 0

    cap = cv2.VideoCapture(str(video))
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
    det = None
    try:
        from . import asd
        det = asd._detector(src_w, src_h, 0.6)
    except Exception:
        det = None  # model missing / unavailable — sharpness fallback still runs

    dur = max(0.1, end - start)
    best_face = None      # (score, ts, cx_frac)
    best_sharp = None     # (sharpness, ts) fallback when no face is ever found
    for i in range(samples):
        ts = start + dur * (i + 0.5) / samples
        cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000.0)
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
        if best_sharp is None or sharp > best_sharp[0]:
            best_sharp = (sharp, ts)
        if det is None:
            continue
        det.setInputSize((frame.shape[1], frame.shape[0]))
        _, faces = det.detect(frame)
        if faces is None:
            continue
        fw_frame = frame.shape[1]
        for f in faces:
            fx, fy, fw, fh, conf = (float(f[0]), float(f[1]), float(f[2]),
                                    float(f[3]), float(f[14]))
            if fw / fw_frame < 0.04:      # ignore tiny background faces
                continue
            score = fw * fh * conf + sharp * 2.0   # big, confident, sharp face wins
            if best_face is None or score > best_face[0]:
                best_face = (score, ts, (fx + fw / 2) / fw_frame)
    cap.release()
    if best_face:
        return best_face[1], best_face[2], src_w, src_h
    if best_sharp:
        return best_sharp[1], None, src_w, src_h
    return (start + end) / 2.0, None, src_w, src_h


def _fill_crop(w: int, h: int, src_w: int, src_h: int,
               cx_frac: float | None) -> str:
    """ffmpeg scale+crop that fills w×h. When the source is wider than the target
    (a 16:9 → 9:16 vertical thumbnail cuts the sides), position the crop window on
    the face's `cx_frac` so the person survives the crop instead of being cut off."""
    if src_w and src_h and cx_frac is not None and (src_w / src_h) > (w / h):
        sw = int(round(h * src_w / src_h) / 2) * 2      # scaled width (height binds)
        cropx = int(min(max(cx_frac * sw - w / 2, 0), max(0, sw - w)))
        return f"scale={sw}:{h},crop={w}:{h}:{cropx}:0"
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def _thumb_ass(title: str, cfg: Config, path: Path, w: int, h: int) -> None:
    tc = cfg.raw.get("thumbnail", {})
    font = tc.get("font", "Arial Black")
    size = int(tc.get("font_size", 72) * (w / 1280))
    primary = _ass_color(tc.get("text_color", "#FFFFFF"))
    box = _ass_color(tc.get("box_color", "#000000"), tc.get("box_opacity", 0.55))
    align = {"top": 8, "center": 5, "bottom": 2}.get(tc.get("position", "bottom"), 2)
    side = int(0.05 * w)
    pad = max(10, int(20 * (w / 1280)))
    mv = int(tc.get("margin", 0.08) * h)
    text = title.strip()
    if tc.get("uppercase", True):
        text = text.upper()
    max_chars = max(10, int((w - 2 * side - 2 * pad) / (size * 0.60)))
    header = (
        "[Script Info]\n"
        f"ScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV, Encoding\n"
        f"Style: Th,{font},{size},{primary},{box},&H00000000,-1,0,3,{pad},0,"
        f"{align},{side},{side},{mv},1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    line = (f"Dialogue: 0,0:00:00.00,0:00:10.00,Th,,0,0,0,,"
            f"{_wrap(_esc(text), max_chars)}")
    path.write_text(header + line + "\n", encoding="utf-8")


def make_thumbnail(video: Path, clip: dict, cfg: Config, out_path: Path) -> Path:
    tc = cfg.raw.get("thumbnail", {})
    w = int(tc.get("width", 1280))
    h = int(tc.get("height", 720))
    ts, cx_frac, src_w, src_h = _best_frame(
        video, float(clip["start"]), float(clip["end"]), int(tc.get("samples", 25)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent
    vf = _fill_crop(w, h, src_w, src_h, cx_frac)

    title = clip.get("title", "")
    prefix = cfg.raw.get("title", {}).get("prefix", "").strip()
    full_title = f"{prefix} {title}".strip() if prefix else title
    if tc.get("show_title", True) and full_title.strip():
        ass = work / (out_path.stem + ".thumb.ass")
        _thumb_ass(full_title, cfg, ass, w, h)
        vf += f",subtitles={ass.name}"

    cmd = [cfg.ffmpeg, "-y", "-ss", f"{ts:.3f}", "-i", str(video),
           "-frames:v", "1", "-vf", vf, "-q:v", "3", str(out_path)]
    proc = subprocess.run(cmd, cwd=work, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError("thumbnail ffmpeg failed:\n"
                           + "\n".join(proc.stdout.splitlines()[-15:]))
    print(f"[thumb] {out_path.name}  from t={ts:.1f}s")
    return out_path


def make_title_thumbnail(video: Path, clip: dict, cfg: Config, out_path: Path,
                         w: int, h: int, card_png: Path | None = None) -> Path:
    """Vertical (9:16) thumbnail: the clearest face frame from the source, fit to
    w x h, with the SAME yellow title card that's burned onto the clip overlaid at
    the render position. Reuses the clip's existing `.title.png` when given; else
    renders the card fresh. No captions, no TTS-freeze blur (the frame is pulled
    from the source, not the rendered mp4)."""
    tc = cfg.raw.get("thumbnail", {})
    start, end = float(clip["start"]), float(clip["end"])
    ts, cx_frac, src_w, src_h = _best_frame(video, start, end, int(tc.get("samples", 25)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = out_path.parent

    card = Path(card_png) if card_png else None
    tmp_card: Path | None = None
    if not (card and card.exists()):
        from . import titlecard
        tmp_card = work / (out_path.stem + ".titlecard.png")
        card = titlecard.render_card(clip.get("title", ""), w, cfg, tmp_card)

    tmv = int(float(cfg.raw.get("title", {}).get("margin", 0.15)) * h)
    bg = _fill_crop(w, h, src_w, src_h, cx_frac)
    if card and Path(card).exists():
        cmd = [cfg.ffmpeg, "-y", "-ss", f"{ts:.3f}", "-i", str(video),
               "-i", str(card), "-filter_complex",
               f"[0:v]{bg}[b];[b][1:v]overlay=(W-w)/2:H-h-{tmv}",
               "-frames:v", "1", "-q:v", "3", str(out_path)]
    else:
        cmd = [cfg.ffmpeg, "-y", "-ss", f"{ts:.3f}", "-i", str(video),
               "-vf", bg, "-frames:v", "1", "-q:v", "3", str(out_path)]
    proc = subprocess.run(cmd, cwd=work, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    if tmp_card and tmp_card.exists():
        tmp_card.unlink()
    if proc.returncode != 0:
        raise RuntimeError("title thumbnail ffmpeg failed:\n"
                           + "\n".join(proc.stdout.splitlines()[-15:]))
    print(f"[thumb] {out_path.name}  from t={ts:.1f}s")
    return out_path
