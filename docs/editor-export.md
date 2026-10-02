# Editor-project export (DaVinci Resolve / Premiere / FCP / CapCut)

The pipeline's normal output is a **baked MP4** — ffmpeg flattens the 9:16 reframe,
the karaoke captions, the title card and the TTS hook into pixels. Once rendered,
none of that is editable.

Turning on `[export]` writes an **editor project alongside** each MP4 so you can
open the cut in a real editor, hand-tweak it, and export from there. It does **not**
replace the MP4 — you get both.

```toml
[export]
enabled = true
formats = ["fcpxml", "otio", "capcut"]
```

Projects land in a sibling **`edit/`** folder next to `clips/`:

```
<job>/
  clips/   clip_01_mobile.mp4      # baked, post-ready (unchanged)
  edit/    clip_01_mobile.fcpxml   # DaVinci / Premiere / FCP
           clip_01_mobile.otio     # OpenTimelineIO
           clip_01_mobile_capcut/  # CapCut draft folder
           clip_01_mobile.manifest.json  # the neutral edit description
```

## This is a re-description, not a pixel-identical re-import

Editor interchange formats carry cuts, transforms, plain-text titles and audio
tracks. They do **not** carry ASS karaoke, ffmpeg crop expressions, or gblur. So
the export is a faithful **starting point** for hand-editing, not a byte-for-byte
rebuild of the MP4.

| Element | Survives? | Notes |
|---|---|---|
| Source in/out (the cut) | ✅ | Exact. References the original source file. |
| 9:16 reframe crop + pan path | ✅ (FCPXML) | Emitted as a scale + keyframed position transform. |
| TTS voiceover | ✅ | As a separate audio track referencing the `.voice.mp3`. |
| Captions | ⚠️ | Plain per-line text clips (FCPXML) / markers (OTIO) / text stickers (CapCut). **No word-by-word highlight.** |
| Title card | ⚠️ | Editor text, not the rendered PNG. |
| Cold-open replay | ⚠️ | Two back-to-back source segments; edit points preserved. |
| Freeze-blur TTS intro | ❌ | The leading segment is a normal cut, not a held/blurred frame. |
| Triptych / stack composite | ❌ | ffmpeg-only; export carries the base cut only. |
| loudnorm / audio fades | ❌ | ffmpeg-only; re-apply in the editor. |

## Per target

- **FCPXML** (`.fcpxml`, FCPXML 1.9) — opens in **DaVinci Resolve**, **Premiere Pro**
  and **Final Cut Pro**. Reliable, source-referenced. Import fidelity of transforms
  and titles varies slightly by app version; Resolve is the most forgiving.
  Connected captions/title use spine-level lanes — Resolve accepts these; strict FCP
  may want them re-anchored.
- **OTIO** (`.otio`, OpenTimelineIO) — needs `pip install opentimelineio`. Captions
  are timeline markers (text lives in the marker name). If the lib isn't installed
  the writer logs a note and skips; FCPXML/CapCut are unaffected.
- **CapCut** (`_capcut/` folder) — **EXPERIMENTAL.** CapCut has no public import
  format; this targets the reverse-engineered `draft_content.json`, which is
  undocumented, **changes between CapCut versions**, and differs desktop vs mobile.
  Crop keyframes are **not** carried. To try it, copy the `_capcut` folder into
  CapCut's drafts directory (Windows desktop:
  `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft\`) and reopen CapCut.
  Expect to fix up segments/text by hand.

## How it's wired

`clipcraft/export.py` builds one neutral `manifest` dict per clip from the values
`render.py` already computes — the reframe crop body (`{cw, ch, cy, src_w, kfs}`),
the caption `events`, the title and the TTS mp3 — then three thin writers emit
FCPXML / OTIO / CapCut off it. Export runs inside `render_clip` (guarded by
`export.enabled`) so it reuses the crop analysis instead of recomputing it, and any
writer failure is logged and skipped without failing the render.
