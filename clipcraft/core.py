"""Assemble a CORE: a rapid montage of many iconic moments from ONE context
(a film / show / person), stitched into a single vertical video.

A CORE is a different output type from a clip (see render.py). A clip is one
continuous moment from one source with a cold-open hook, a title card and
karaoke captions. A CORE takes N pre-picked scene files, drops each into a
centered cinematic letterbox band, burns a single persistent "<LABEL> CORE:"
tag across the whole montage, keeps the scene dialogue under an optional music
bed, and ends on a full-frame promo card. There are no per-scene titles, no
cold open and no captions — the format is the letterbox band + the tag.

The visual constants are locked to the reference (@brosklipp21 "PESTA COPET
CORE"): a full-width band that is 0.9x the width tall (720x648 on a 720x1280
frame -> symmetric black bars), and the tag centered at ~0.54 of the height.
They live in [core] in config.toml so a project can override them.

Assembly is manifest-driven: the user lists the scenes (and optional in/out
trims) in a JSON manifest. Auto-selecting the beats is a later layer that can
reuse select.py per file.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import Config
from .render import _ass_color, _esc, _run, _fmt_ass_time

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
_LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"


def _has_audio(ffprobe: str, path: Path) -> bool:
    if not ffprobe:
        return True  # assume yes; the silent-fallback is only an optimisation
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return bool(proc.stdout.strip())


def _probe_dur(ffprobe: str, path: Path) -> float:
    if not ffprobe:
        return 0.0
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def _geometry(cfg: Config, w: int, h: int) -> tuple[int, int]:
    """(band_height, top_offset) for the centered cinematic band."""
    ratio = float(cfg.raw.get("core", {}).get("band_height_ratio", 0.9))
    band_h = int(round(w * ratio))
    band_h -= band_h % 2
    band_h = min(band_h, h)
    offset = (h - band_h) // 2
    offset -= offset % 2
    return band_h, offset


def _write_label_ass(cfg: Config, path: Path, label: str, w: int, h: int,
                     windows: list[tuple[float, float]] | None = None) -> None:
    """Burn the "<label> CORE:" tag. With `windows` (list of (start,end) secs),
    the tag flashes only during those spans — used to punch it in at each
    transition, synced with the whoosh. Without windows, it stays on the whole
    time (the persistent look)."""
    ccfg = cfg.raw.get("core", {})
    scale = w / 1080
    size = int(ccfg.get("label_font_size", 60) * scale)
    primary = _ass_color(ccfg.get("label_color", "#FFFFFF"))
    outline_c = _ass_color(ccfg.get("label_outline_color", "#000000"))
    bold = -1 if ccfg.get("label_bold", True) else 0
    italic = -1 if ccfg.get("label_italic", True) else 0
    outline = max(1, int(ccfg.get("label_outline_width", 3) * scale))
    y = int(float(ccfg.get("label_y", 0.539)) * h)
    suffix = ccfg.get("label_suffix", " CORE:")
    text = f"{label.strip()}{suffix}"
    if ccfg.get("label_uppercase", True):
        text = text.upper()
    font = ccfg.get("label_font", "Arial Black")
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
        "Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV, Encoding\n"
        f"Style: Core,{font},{size},{primary},{outline_c},&H00000000,"
        f"{bold},{italic},1,{outline},0,5,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    pos = f"{{\\pos({w // 2},{y})}}{_esc(text)}"
    if windows:
        lines = [f"Dialogue: 0,{_fmt_ass_time(s)},{_fmt_ass_time(e)},Core,,0,0,0,,{pos}"
                 for s, e in windows]
    else:
        lines = [f"Dialogue: 0,0:00:00.00,9:59:59.99,Core,,0,0,0,,{pos}"]
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")


def _norm_scene(cfg: Config, src: Path, t_in: float | None, t_out: float | None,
                w: int, h: int, band_h: int, offset: int, fps: int,
                out: Path) -> None:
    """Cut [t_in, t_out] from `src`, cover-crop it into the centered band on a
    black w x h canvas, and normalise codec/fps/SAR/audio so the segments concat
    cleanly."""
    vf = (f"scale={w}:{band_h}:force_original_aspect_ratio=increase,"
          f"crop={w}:{band_h},pad={w}:{h}:0:{offset}:color=black,"
          f"fps={fps},setsar=1,format=yuv420p")
    cmd = [cfg.ffmpeg, "-y"]
    if t_in is not None:
        cmd += ["-ss", f"{float(t_in):.3f}"]
    cmd += ["-i", str(src)]
    if t_out is not None:
        dur = float(t_out) - float(t_in or 0.0)
        if dur <= 0:
            raise ValueError(f"scene out ({t_out}) must be > in ({t_in}) for {src.name}")
        cmd += ["-t", f"{dur:.3f}"]
    has_audio = _has_audio(cfg.ffprobe, src)
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += [
        "-map", "0:v:0", "-map", ("1:a:0" if not has_audio else "0:a:0?"),
        "-vf", vf, "-r", str(fps), "-af", "aresample=44100", "-ac", "2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
    ]
    if not has_audio:
        cmd += ["-shortest"]
    cmd += [str(out)]
    _run(cmd)


def _norm_endcard(cfg: Config, src: Path, w: int, h: int, fps: int,
                  seconds: float, out: Path) -> None:
    """Fit the end card to the full w x h frame (no band, no tag) and normalise
    to match the body so it concats cleanly. Images are held for `seconds`."""
    is_image = src.suffix.lower() in _IMAGE_EXTS
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
          f"fps={fps},setsar=1,format=yuv420p")
    cmd = [cfg.ffmpeg, "-y"]
    if is_image:
        cmd += ["-loop", "1", "-t", f"{seconds:.3f}", "-i", str(src),
                "-f", "lavfi", "-t", f"{seconds:.3f}",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-i", str(src)]
        if _has_audio(cfg.ffprobe, src):
            cmd += ["-map", "0:v:0", "-map", "0:a:0"]
        else:
            cmd += ["-f", "lavfi", "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    cmd += [
        "-vf", vf, "-r", str(fps), "-af", "aresample=44100", "-ac", "2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
        str(out),
    ]
    _run(cmd)


def _prep_transition_audio(cfg: Config, sfx: Path, out: Path, vol: float) -> float:
    """Strip the leading/trailing silence off the transition SFX so only the
    whoosh itself remains, at `vol`. Returns the trimmed duration in seconds."""
    trim = ("silenceremove=start_periods=1:start_threshold=-50dB:detection=peak,"
            "areverse,"
            "silenceremove=start_periods=1:start_threshold=-50dB:detection=peak,"
            "areverse")
    _run([cfg.ffmpeg, "-y", "-i", str(sfx), "-af",
          f"{trim},volume={vol},aresample=44100", "-ac", "2", str(out)])
    return _probe_dur(cfg.ffprobe, out)


def _interstitial_clip(cfg: Config, style: str, seg: Path, sfx_wav: Path,
                       dur: float, w: int, h: int, fps: int, out: Path) -> None:
    """A held transition of length `dur`: either FREEZE the outgoing beat's last
    frame (style="freeze") or a BLACK screen (style="black"), while the whoosh
    plays out fully — then the montage cuts to the next beat. The tag is drawn
    later in the body pass. Encoded to match the segments so it concats cleanly."""
    vf = f"fps={fps},setsar=1,format=yuv420p"
    if style == "black":
        vin = ["-f", "lavfi", "-t", f"{dur:.3f}",
               "-i", f"color=c=black:s={w}x{h}:r={fps}"]
    else:  # freeze the outgoing last frame
        frame = out.with_suffix(".png")
        _run([cfg.ffmpeg, "-y", "-sseof", "-0.15", "-i", str(seg),
              "-update", "1", "-frames:v", "1", "-q:v", "2", str(frame)])
        vin = ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(frame)]
    fc = f"[0:v]{vf}[v];[1:a]apad,atrim=0:{dur:.3f},aresample=44100[a]"
    _run([cfg.ffmpeg, "-y", *vin, "-i", str(sfx_wav),
          "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
          "-r", str(fps), "-t", f"{dur:.3f}",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
          str(out)])


def _concat_list(paths: list[Path], list_path: Path) -> None:
    lines = ["file '" + str(p.resolve()).replace("'", "'\\''") + "'" for p in paths]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resolve_scene(path: str, project_dir: Path | None, manifest_dir: Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        if p.exists():
            return p
        raise FileNotFoundError(f"scene not found: {p}")
    cands = []
    if project_dir:
        cands += [project_dir / "source" / path, project_dir / path]
    cands += [manifest_dir / path, Path.cwd() / path]
    for c in cands:
        if c.exists():
            return c
    raise FileNotFoundError(
        f"scene '{path}' not found (looked in "
        f"{', '.join(str(c.parent) for c in cands)})")


def build_core(manifest: dict, cfg: Config, out_path: Path, w: int, h: int,
               project_dir: Path | None, manifest_dir: Path,
               work: Path | None = None) -> Path:
    """Assemble one CORE described by `manifest` to `out_path` at w x h.

    Manifest schema:
        {
          "label": "PESTA COPET",              # -> "PESTA COPET CORE:" tag
          "end_card": "CTA SEDANG TAYANG.mp4",  # optional 9:16 promo asset
          "end_card_seconds": 4,                # hold time if end_card is an image
          "music": "bed.mp3",                   # optional music bed
          "scenes": [
            {"file": "01. KEJAR.mp4", "in": 2.0, "out": 12.0},
            {"file": "03. HAPE TERBANG.mp4"}
          ]
        }
    """
    if not cfg.ffmpeg:
        raise RuntimeError("ffmpeg not found. Run `python -m clipcraft doctor`.")
    scenes = manifest.get("scenes") or []
    if not scenes:
        raise ValueError("manifest has no scenes.")
    label = str(manifest.get("label", "")).strip()
    if not label:
        raise ValueError("manifest needs a non-empty 'label' (the CORE tag).")

    ccfg = cfg.raw.get("core", {})
    fps = int(ccfg.get("fps", 30))
    band_h, offset = _geometry(cfg, w, h)
    # Absolute: the body pass runs with cwd=work (so the subtitles filter can
    # reference the .ass by name), which would otherwise break a relative output.
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = (work or out_path.parent / (out_path.stem + ".build")).resolve()
    work.mkdir(parents=True, exist_ok=True)

    print(f"[core] {out_path.name}: {len(scenes)} scenes, band {w}x{band_h} "
          f"(bars {offset}px), tag \"{label} CORE:\"")

    seg_paths: list[Path] = []
    for i, sc in enumerate(scenes, 1):
        src = _resolve_scene(sc["file"], project_dir, manifest_dir)
        seg = work / f"seg_{i:02d}.mp4"
        t_in = sc.get("in")
        t_out = sc.get("out")
        print(f"[core]   scene {i:02d}: {src.name}"
              + (f"  [{t_in}-{t_out}]" if t_in is not None or t_out is not None else ""))
        _norm_scene(cfg, src, t_in, t_out, w, h, band_h, offset, fps, seg)
        seg_paths.append(seg)

    end_card = manifest.get("end_card")
    music_path = None
    if manifest.get("music"):
        music_path = _resolve_scene(manifest["music"], project_dir, manifest_dir)
    trans_ref = manifest.get("transition") or ccfg.get("transition_file")
    trans_path = _resolve_scene(trans_ref, project_dir, manifest_dir) if trans_ref else None

    # A transition is a HELD interstitial: at each cut the montage freezes the
    # outgoing frame (or goes black) and holds while the whoosh plays out fully,
    # showing the "<label> CORE:" tag, THEN cuts to the next beat. The interstitial
    # clips are interleaved into the concat timeline; the tag is drawn only over
    # their time spans (`windows`), so the beats themselves stay clean.
    durs = [_probe_dur(cfg.ffprobe, p) for p in seg_paths]
    tag_mode = ccfg.get("tag_mode", "transition")
    style = manifest.get("transition_style", ccfg.get("transition_style", "freeze"))

    timeline: list[Path] = []
    windows: list[tuple[float, float]] = []
    n_trans = 0
    sfx_wav = None
    if trans_path:
        sfx_wav = work / "trans_sfx.wav"
        whoosh = _prep_transition_audio(cfg, trans_path, sfx_wav,
                                        float(ccfg.get("transition_volume", 0.9)))
        int_dur = whoosh + float(ccfg.get("transition_tail", 0.05))
    last_idx = len(seg_paths) - 1
    t = 0.0
    for i, seg in enumerate(seg_paths):
        timeline.append(seg)
        t += durs[i]
        if sfx_wav is not None and (i < last_idx or end_card):
            inter = work / f"trans_{i + 1:02d}.mp4"
            _interstitial_clip(cfg, style, seg, sfx_wav, int_dur, w, h, fps, inter)
            timeline.append(inter)
            if tag_mode == "transition":
                windows.append((t, t + int_dur))  # tag shows during the hold
            t += int_dur
            n_trans += 1

    segs_txt = work / "segs.txt"
    _concat_list(timeline, segs_txt)

    ass_path = work / "core.ass"
    _write_label_ass(cfg, ass_path, label, w, h,
                     windows=(windows if tag_mode == "transition" else None))

    body = work / "body.mp4"
    cmd = [cfg.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(segs_txt.resolve())]
    fparts = [f"[0:v]subtitles={ass_path.name}[v]"]
    if music_path:
        cmd += ["-stream_loop", "-1", "-i", str(music_path)]
        mvol = float(ccfg.get("music_volume", 0.25))
        fparts.append(f"[0:a]volume=1[d];[1:a]volume={mvol}[mus];"
                      f"[d][mus]amix=inputs=2:duration=first:normalize=0,{_LOUDNORM}[a]")
    else:
        fparts.append(f"[0:a]{_LOUDNORM}[a]")
    cmd += [
        "-filter_complex", ";".join(fparts), "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
        "-movflags", "+faststart", str(body),
    ]
    extras = []
    if music_path:
        extras.append("music bed")
    if n_trans:
        extras.append(f"{n_trans} {style}+tag holds")
    print(f"[core]   body: {tag_mode} tag"
          + (" + " + " + ".join(extras) if extras else "") + " over montage")
    _run(cmd, cwd=work)

    parts = [body]
    if end_card:
        end_src = _resolve_scene(end_card, project_dir, manifest_dir)
        end = work / "end.mp4"
        secs = float(manifest.get("end_card_seconds",
                                  ccfg.get("end_card_seconds", 4)))
        print(f"[core]   end card: {end_src.name}")
        _norm_endcard(cfg, end_src, w, h, fps, secs, end)
        parts.append(end)

    if len(parts) == 1:
        body.replace(out_path)
    else:
        final_txt = work / "final.txt"
        _concat_list(parts, final_txt)
        _run([cfg.ffmpeg, "-y", "-f", "concat", "-safe", "0",
              "-i", str(final_txt.resolve()), "-c", "copy",
              "-movflags", "+faststart", str(out_path)])
    print(f"[core] done -> {out_path}")
    return out_path


def load_manifest(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
