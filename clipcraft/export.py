"""Editor-project export: hand the same cut off to DaVinci Resolve / Premiere
(FCPXML), OpenTimelineIO, and CapCut — *alongside* the baked MP4, not instead of
it.

The pipeline in `render.py` flattens everything into pixels with ffmpeg. Editor
interchange formats carry cuts, transforms, plain-text titles and audio tracks —
they do NOT carry ASS karaoke, ffmpeg crop expressions, or gblur. So this module
is deliberately a *best-effort re-description* of the edit, not a pixel-identical
re-import:

  survives well : source in/out (the cut), the 9:16 reframe crop + its pan path,
                  the TTS voiceover as a second audio track.
  degrades      : captions become plain per-line text clips (no word-by-word
                  highlight); the title card becomes editor text, not the PNG.
  dropped       : cold-open concat, freeze-blur intro, triptych composite,
                  loudnorm — these are ffmpeg-only and have no interchange peer.

Everything flows from one neutral `manifest` dict (`build_manifest`) so the three
writers stay thin. CapCut has no open import format — its writer targets the
undocumented `draft_content.json` and is EXPERIMENTAL: it can break between
CapCut versions and differs desktop vs mobile.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from .config import Config


def enabled(cfg: Config) -> bool:
    return bool(cfg.raw.get("export", {}).get("enabled", False))


def wanted_formats(cfg: Config) -> list[str]:
    ex = cfg.raw.get("export", {})
    fmts = ex.get("formats", ["fcpxml", "otio", "capcut"])
    return [str(f).lower().strip() for f in fmts if str(f).strip()]


def _probe_source(ffprobe: str, video: Path) -> tuple[int, int, float]:
    """(width, height, fps) of the source's first video stream. Falls back to
    1920x1080@30 if ffprobe is unavailable so export never hard-fails a render."""
    if not ffprobe:
        return 1920, 1080, 30.0
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "json", str(video)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
        st = (data.get("streams") or [{}])[0]
        w = int(st.get("width") or 1920)
        h = int(st.get("height") or 1080)
        num, _, den = str(st.get("r_frame_rate", "30/1")).partition("/")
        fps = (float(num) / float(den)) if den and float(den) else float(num or 30)
        return w, h, round(fps, 6) or 30.0
    except Exception:
        return 1920, 1080, 30.0


def build_manifest(*, clip: dict, cfg: Config, video: Path, out_stem: str,
                   out_w: int, out_h: int, total: float,
                   src_start: float, src_end: float,
                   video_segments: list[tuple[float, float, float]],
                   crop: dict | None, events: list[dict],
                   title_text: str, tts_path: Path | None,
                   tts_start: float) -> dict:
    """Neutral, editor-agnostic description of one clip's edit.

    `video_segments` is the timeline as [(timeline_start, source_in, duration)]
    in output seconds — one entry normally, two when a cold-open replays a moment.
    `crop` is the reframe body ({cw, ch, cy, src_w, kfs}) or None (no reframe).
    `events` is the caption list ({start, end, text}) already timed to the output.
    """
    sw, sh, sfps = _probe_source(cfg.ffprobe, video)
    crop_out = None
    if crop:
        cw = int(crop.get("cw") or sw)
        ch = int(crop.get("ch") or sh)
        cy = int(crop.get("cy") or 0)
        kfs = crop.get("kfs") or [(0.0, max(0, (sw - cw) // 2))]
        crop_out = {
            "cw": cw, "ch": ch, "cy": cy, "src_w": int(crop.get("src_w") or sw),
            "keyframes": [{"t": round(float(t), 3), "x": int(x)} for t, x in kfs],
        }

    captions = [
        {"start": round(float(e["start"]), 3), "end": round(float(e["end"]), 3),
         "text": (e.get("text") or "").strip()}
        for e in (events or []) if (e.get("text") or "").strip()
    ]

    audio = [{"role": "main", "timeline_start": round(video_segments[0][0], 3),
              "source_in": round(src_start, 3),
              "duration": round(sum(s[2] for s in video_segments), 3)}]
    if tts_path is not None and Path(tts_path).exists():
        audio.append({"role": "tts", "path": str(Path(tts_path).resolve()),
                      "timeline_start": round(float(tts_start), 3)})

    return {
        "clip_title": clip.get("title", ""),
        "hook": clip.get("hook", ""),
        "out_stem": out_stem,
        "source": {"path": str(Path(video).resolve()),
                   "width": sw, "height": sh, "fps": sfps},
        "output": {"width": out_w, "height": out_h, "fps": sfps,
                   "duration": round(float(total), 3)},
        "timeline": {
            "video": [{"timeline_start": round(ts, 3), "source_in": round(si, 3),
                       "duration": round(d, 3)} for ts, si, d in video_segments],
            "crop": crop_out,
            "captions": captions,
            "title_card": ({"text": title_text.strip(), "start": 0.0}
                           if title_text.strip() else None),
            "audio": audio,
        },
    }


# ---------------------------------------------------------------------------
# FCPXML 1.9  (DaVinci Resolve, Premiere Pro, Final Cut Pro)
# ---------------------------------------------------------------------------

def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _rational(sec: float, fps: float) -> str:
    """Frame-aligned FCPXML time value, e.g. '60000/30000s' for 2s @30fps."""
    den = int(round(fps * 1000)) or 30000
    frames = int(round(sec * fps))
    return f"{frames * 1000}/{den}s"


def _frame_dur(fps: float) -> str:
    den = int(round(fps * 1000)) or 30000
    return f"1000/{den}s"


def write_fcpxml(manifest: dict, path: Path) -> Path:
    """Well-formed FCPXML 1.9. Resolve/Premiere/FCP all read this; each honours a
    slightly different subset (transforms and titles vary by version)."""
    src = manifest["source"]
    out = manifest["output"]
    fps = out["fps"] or 30.0
    tl = manifest["timeline"]
    ow, oh = out["width"], out["height"]

    seq_dur = _rational(out["duration"], fps)
    fd = _frame_dur(fps)
    asset_id = "r2"
    asset_dur = _rational(max(src.get("dur_hint", 3600.0), out["duration"]), fps)
    src_uri = Path(src["path"]).as_uri()

    # Reframe crop -> a static transform (scale to fill height + pan to the first
    # keyframe). Keyframed pan is emitted as adjust-transform position keyframes.
    transform = ""
    crop = tl.get("crop")
    if crop and crop["cw"] < src["width"]:
        scale = oh / crop["ch"] if crop["ch"] else 1.0
        # FCPXML position is in points from centre; +x moves the image left in
        # frame, so pan = (centre_of_source - centre_of_crop) * scale.
        kfs = crop["keyframes"] or [{"t": 0.0, "x": (src["width"] - crop["cw"]) // 2}]
        half_src = src["width"] / 2.0

        def _posx(x):
            crop_centre = x + crop["cw"] / 2.0
            return round((half_src - crop_centre) * scale, 2)

        if len(kfs) <= 1:
            transform = (f'\n                    <adjust-transform '
                         f'position="{_posx(kfs[0]["x"])} 0" '
                         f'scale="{scale:.4f} {scale:.4f}"/>')
        else:
            keyframes = "".join(
                f'\n                                <keyframe time="{_rational(k["t"], fps)}" '
                f'value="{_posx(k["x"])} 0"/>' for k in kfs)
            transform = (
                f'\n                    <adjust-transform scale="{scale:.4f} {scale:.4f}">'
                f'\n                        <param name="position">'
                f'{keyframes}'
                f'\n                        </param>'
                f'\n                    </adjust-transform>')

    # Primary video/audio: one asset-clip per segment on the spine.
    spine = []
    t_cursor = 0.0
    for seg in tl["video"]:
        off = _rational(seg["timeline_start"], fps)
        st = _rational(seg["source_in"], fps)
        du = _rational(seg["duration"], fps)
        spine.append(
            f'\n                <asset-clip name="{_xml_escape(manifest["out_stem"])}" '
            f'ref="{asset_id}" offset="{off}" start="{st}" duration="{du}" '
            f'format="r1" tcFormat="NDF">{transform}'
            f'\n                </asset-clip>')
        t_cursor += seg["duration"]

    # Captions -> connected <title> clips (lane 1). Plain text; no karaoke.
    titles = []
    for i, cap in enumerate(tl["captions"]):
        off = _rational(cap["start"], fps)
        du = _rational(max(cap["end"] - cap["start"], 1.0 / fps), fps)
        txt = _xml_escape(cap["text"])
        titles.append(
            f'\n                <title name="cap{i}" lane="1" offset="{off}" '
            f'ref="r3" duration="{du}" start="0s">'
            f'\n                    <text><text-style ref="ts{i}">{txt}</text-style></text>'
            f'\n                    <text-style-def id="ts{i}">'
            f'\n                        <text-style font="Helvetica" fontSize="72" '
            f'fontColor="1 1 1 1" bold="1" alignment="center"/>'
            f'\n                    </text-style-def>'
            f'\n                </title>')

    # Title card -> one more connected title clip on lane 2.
    tc = tl.get("title_card")
    if tc:
        du = _rational(min(2.5, out["duration"]), fps)
        titles.append(
            f'\n                <title name="titlecard" lane="2" offset="{_rational(0.0, fps)}" '
            f'ref="r3" duration="{du}" start="0s">'
            f'\n                    <text><text-style ref="tsc">{_xml_escape(tc["text"])}</text-style></text>'
            f'\n                    <text-style-def id="tsc">'
            f'\n                        <text-style font="Helvetica" fontSize="64" '
            f'fontColor="0.07 0.07 0.07 1" bold="1" alignment="center"/>'
            f'\n                    </text-style-def>'
            f'\n                </title>')

    # TTS voiceover -> connected audio clip referencing the mp3 as its own asset.
    tts_asset = ""
    tts_clip = ""
    for a in tl["audio"]:
        if a["role"] == "tts":
            tts_uri = Path(a["path"]).as_uri()
            tts_asset = (
                f'\n        <asset id="r4" name="voiceover" start="0s" '
                f'hasAudio="1" audioSources="1">'
                f'\n            <media-rep kind="original-media" src="{tts_uri}"/>'
                f'\n        </asset>')
            tts_clip = (
                f'\n                <asset-clip name="voiceover" lane="-1" ref="r4" '
                f'offset="{_rational(a["timeline_start"], fps)}" start="0s" '
                f'duration="{_rational(max(out["duration"] - a["timeline_start"], 0.1), fps)}"/>')

    doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.9">
    <resources>
        <format id="r1" name="FFVideoFormat{ow}x{oh}" frameDuration="{fd}" width="{ow}" height="{oh}"/>
        <asset id="{asset_id}" name="{_xml_escape(Path(src['path']).stem)}" start="0s" duration="{asset_dur}" hasVideo="1" hasAudio="1" format="r1" videoSources="1" audioSources="1">
            <media-rep kind="original-media" src="{src_uri}"/>
        </asset>
        <effect id="r3" name="Text" uid=".../Titles.localized/Basic Text.localized/Text.localized/Text.moti"/>{tts_asset}
    </resources>
    <library>
        <event name="clipcraft">
            <project name="{_xml_escape(manifest['out_stem'])}">
                <sequence format="r1" duration="{seq_dur}" tcFormat="NDF">
                    <spine>{''.join(spine)}{''.join(titles)}{tts_clip}
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
"""
    path.write_text(doc, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# OpenTimelineIO  (Resolve / Premiere via the OTIO adapter, and otiotool)
# ---------------------------------------------------------------------------

def write_otio(manifest: dict, path: Path) -> Path | None:
    """A .otio timeline: video segments as clips on one track, TTS on an audio
    track, captions as timeline markers. Requires `opentimelineio`; returns None
    (with a note) when it isn't installed."""
    try:
        import opentimelineio as otio
    except Exception:
        print("[export] opentimelineio not installed — skipping .otio "
              "(`pip install opentimelineio`).")
        return None

    out = manifest["output"]
    fps = out["fps"] or 30.0
    src = manifest["source"]
    tl = manifest["timeline"]

    timeline = otio.schema.Timeline(name=manifest["out_stem"])
    vtrack = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    ref = otio.schema.ExternalReference(
        target_url=Path(src["path"]).as_uri(),
        available_range=otio.opentime.TimeRange(
            otio.opentime.RationalTime(0, fps),
            otio.opentime.RationalTime(max(out["duration"] * 4, 3600) * fps, fps)))
    for seg in tl["video"]:
        clip = otio.schema.Clip(
            name=manifest["out_stem"], media_reference=ref,
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(seg["source_in"] * fps, fps),
                otio.opentime.RationalTime(seg["duration"] * fps, fps)))
        if tl.get("crop"):
            clip.metadata["clipcraft"] = {"crop": tl["crop"]}
        vtrack.append(clip)
    timeline.tracks.append(vtrack)

    for cap in tl["captions"]:
        m = otio.schema.Marker(
            name=cap["text"],
            marked_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(cap["start"] * fps, fps),
                otio.opentime.RationalTime(max(cap["end"] - cap["start"], 0.04) * fps, fps)))
        vtrack.markers.append(m)

    for a in tl["audio"]:
        if a["role"] != "tts":
            continue
        atrack = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)
        aref = otio.schema.ExternalReference(target_url=Path(a["path"]).as_uri())
        dur = max(out["duration"] - a["timeline_start"], 0.1)
        atrack.append(otio.schema.Clip(
            name="voiceover", media_reference=aref,
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(0, fps),
                otio.opentime.RationalTime(dur * fps, fps))))
        timeline.tracks.append(atrack)

    otio.adapters.write_to_file(timeline, str(path))
    return path


# ---------------------------------------------------------------------------
# CapCut draft  (EXPERIMENTAL — undocumented, version-fragile)
# ---------------------------------------------------------------------------

def _uid() -> str:
    return str(uuid.uuid4()).upper()


def write_capcut(manifest: dict, draft_dir: Path) -> Path:
    """Best-effort CapCut draft folder (`draft_content.json`). EXPERIMENTAL:
    CapCut has no public import format; this targets a reverse-engineered schema
    that changes between versions and differs desktop vs mobile. It lays the
    source segment(s), the TTS audio, and per-line text stickers — no reframe
    keyframes, no karaoke.

    To use: copy `draft_dir` into CapCut's drafts location
    (Windows desktop: %LOCALAPPDATA%\\CapCut\\User Data\\Projects\\com.lveditor.draft\\).
    """
    draft_dir.mkdir(parents=True, exist_ok=True)
    out = manifest["output"]
    fps = out["fps"] or 30.0
    src = manifest["source"]
    tl = manifest["timeline"]
    US = 1_000_000  # CapCut times are microseconds

    def us(sec: float) -> int:
        return int(round(sec * US))

    canvas = {"width": out["width"], "height": out["height"], "ratio": "original"}

    video_mat = {"id": _uid(), "type": "video", "path": src["path"],
                 "material_name": Path(src["path"]).name,
                 "width": src["width"], "height": src["height"],
                 "duration": us(max(out["duration"] * 4, 3600))}
    video_segs = []
    for seg in tl["video"]:
        video_segs.append({
            "id": _uid(), "material_id": video_mat["id"],
            "target_timerange": {"start": us(seg["timeline_start"]),
                                 "duration": us(seg["duration"])},
            "source_timerange": {"start": us(seg["source_in"]),
                                 "duration": us(seg["duration"])},
            "speed": 1.0, "volume": 1.0, "visible": True,
        })

    text_mats, text_segs = [], []
    for cap in tl["captions"]:
        tid = _uid()
        text_mats.append({
            "id": tid, "type": "text", "content": cap["text"],
            "font_size": 8.0, "text_color": "#FFFFFF",
            "alignment": 1, "bold": True})
        text_segs.append({
            "id": _uid(), "material_id": tid,
            "target_timerange": {"start": us(cap["start"]),
                                 "duration": us(max(cap["end"] - cap["start"], 0.04))},
            "clip": {"transform": {"x": 0.0, "y": -0.6}},  # lower third
        })
    tc = tl.get("title_card")
    if tc:
        tid = _uid()
        text_mats.append({"id": tid, "type": "text", "content": tc["text"],
                          "font_size": 9.0, "text_color": "#111111", "bold": True})
        text_segs.append({
            "id": _uid(), "material_id": tid,
            "target_timerange": {"start": 0, "duration": us(min(2.5, out["duration"]))},
            "clip": {"transform": {"x": 0.0, "y": 0.6}}})

    audio_mats, audio_segs = [], []
    for a in tl["audio"]:
        if a["role"] != "tts":
            continue
        aid = _uid()
        dur = max(out["duration"] - a["timeline_start"], 0.1)
        audio_mats.append({"id": aid, "type": "audio", "path": a["path"],
                           "material_name": Path(a["path"]).name, "duration": us(dur)})
        audio_segs.append({
            "id": _uid(), "material_id": aid,
            "target_timerange": {"start": us(a["timeline_start"]), "duration": us(dur)},
            "source_timerange": {"start": 0, "duration": us(dur)},
            "volume": 1.0})

    tracks = [{"type": "video", "segments": video_segs}]
    if text_segs:
        tracks.append({"type": "text", "segments": text_segs})
    if audio_segs:
        tracks.append({"type": "audio", "segments": audio_segs})

    draft = {
        "canvas_config": canvas,
        "duration": us(out["duration"]),
        "fps": fps,
        "materials": {"videos": [video_mat], "texts": text_mats, "audios": audio_mats},
        "tracks": tracks,
        "platform": {"app_source": "clipcraft", "os": "windows"},
        "create_time": 0,
        "_clipcraft_note": "EXPERIMENTAL export — CapCut schema is undocumented "
                           "and version-specific; segments/text may need manual fixup.",
    }
    meta = {"draft_name": manifest["out_stem"], "draft_fold_path": str(draft_dir),
            "tm_draft_create": 0, "draft_id": _uid()}
    (draft_dir / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    (draft_dir / "draft_meta_info.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return draft_dir


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def export_clip(manifest: dict, out_dir: Path, cfg: Config) -> list[str]:
    """Write every enabled editor project for one clip. Never raises into the
    render loop — each writer failure is logged and skipped. Returns the list of
    written paths (for logging / the manifest)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = manifest["out_stem"]
    written: list[str] = []
    for fmt in wanted_formats(cfg):
        try:
            if fmt in ("fcpxml", "davinci", "resolve", "premiere", "fcp"):
                p = write_fcpxml(manifest, out_dir / f"{stem}.fcpxml")
                written.append(str(p))
            elif fmt in ("otio", "opentimelineio"):
                p = write_otio(manifest, out_dir / f"{stem}.otio")
                if p:
                    written.append(str(p))
            elif fmt in ("capcut", "capcut_draft"):
                p = write_capcut(manifest, out_dir / f"{stem}_capcut")
                written.append(str(p))
            else:
                print(f"[export] unknown export format '{fmt}' — skipped.")
        except Exception as exc:
            print(f"[export] {fmt} export failed for {stem}: {exc}")
    if written:
        (out_dir / f"{stem}.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return written
