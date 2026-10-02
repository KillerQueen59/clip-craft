"""ClipCraft CLI — single entry point for all commands."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, config


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", text.strip()).strip("-.")
    return s[:60] or "clip"


def _job_dir(cfg, source: str, name: str | None) -> Path:
    """Per-run output folder: work/<name>/. Name is --name, else derived from
    the source (file stem, or a short id for URLs)."""
    if name:
        base = _slug(name)
    elif Path(source).exists():
        base = _slug(Path(source).stem)
    else:
        from .ingest import content_id
        base = "yt_" + content_id(source)
    d = cfg.work_dir / base
    d.mkdir(parents=True, exist_ok=True)
    return d


def _print_table(cands: list[dict]) -> None:
    if not cands:
        print("(no candidates)")
        return
    print(f"\n{'#':>2}  {'score':>5}  {'start':>7}  {'end':>7}  {'len':>5}  hook")
    print("-" * 78)
    for i, c in enumerate(cands, 1):
        length = c.get("duration", c["end"] - c["start"])
        hook = (c.get("hook") or "").replace("\n", " ")[:44]
        print(f"{i:>2}  {c.get('score', 0):>5.1f}  {c['start']:>7.2f}  "
              f"{c['end']:>7.2f}  {length:>5.1f}  {hook}")
        if c.get("risks"):
            print(f"      risks: {'; '.join(c['risks'])[:70]}")
    print()


def _load_brief(cfg, brief_arg: str | None) -> str:
    """Brief text from --brief (file path or inline text), else config
    brief_file, else the project's brief.txt."""
    pdir = getattr(cfg, "project_dir", None)
    src = brief_arg or cfg.select.get("brief_file", "")
    if not src and pdir and (pdir / "brief.txt").exists():
        return (pdir / "brief.txt").read_text(encoding="utf-8")
    if not src:
        return ""
    for cand in ([Path(src), pdir / src] if pdir else [Path(src)]):
        if cand and cand.exists():
            return cand.read_text(encoding="utf-8")
    return src  # treat as inline brief text


def _pipeline_to_candidates(source: str, cfg, n: int,
                            brief: str = "") -> tuple[Path, dict, list[dict]]:
    from .ingest import ingest
    from .transcribe import transcribe
    from .select import select
    from .snap import snap_all
    from . import reaction

    video = ingest(source, cfg)
    print(f"[cli] source -> {video}")
    transcript = transcribe(video, cfg)
    cands = select(transcript, cfg, n, brief=brief)
    # Viewer-POV context guard: pull start back to include the setup a clip
    # depends on (dangling opener or a model-flagged topic dependency); demote
    # clips whose context is out of reach so self-contained clips win top slots.
    from .select import apply_context_guard
    cands = apply_context_guard(cands, transcript)
    n_ctx = sum(1 for c in cands if c.get("_context_expanded"))
    n_dem = sum(1 for c in cands if c.get("context_unresolved"))
    if n_ctx:
        print(f"[select] context guard: pulled start earlier on {n_ctx} clip(s) "
              "to include their setup")
    if n_dem:
        print(f"[select] context guard: demoted {n_dem} clip(s) whose context is "
              "out of reach (flagged context_unresolved)")
    # Audio reaction: promote moments the room laughed at (never drops any),
    # then let boundary snapping run, then land the out-point on the laugh.
    cands = reaction.annotate(cands, video, cfg)
    cands = snap_all(cands, transcript, cfg)
    cands = reaction.snap_to_laugh(cands, cfg)
    # House rule: no two shipped clips may share source time. Snapping + laugh
    # snapping shift boundaries and can push adjacent clips into each other, so
    # resolve overlaps last, keeping the stronger clip on each collision.
    from .select import resolve_overlaps
    before = len(cands)
    cands = resolve_overlaps(cands, cfg.clip.get("min_seconds", 10))
    if len(cands) < before:
        print(f"[cli] overlap guard: dropped {before - len(cands)} "
              f"overlapping candidate(s)")
    cands.sort(key=lambda c: c.get("score", 0), reverse=True)
    return video, transcript, cands


PROJECTS_DIR = config.ROOT / "projects"


def _project_dir(name: str) -> Path:
    return PROJECTS_DIR / name.strip()


def _load_cfg(args):
    """Load config, honoring --project (self-contained project folder) or
    --config (a single override file)."""
    project = getattr(args, "project", None)
    if project:
        pdir = _project_dir(project)
        if not pdir.exists():
            raise FileNotFoundError(
                f'project not found: {pdir}\nCreate it first:  '
                f'py -3 -m clipcraft init "{project}"')
        cfgp = pdir / "config.toml"
        print(f"[cli] project: {pdir}")
        cfg = config.load(cfgp if cfgp.exists() else None)
        cfg.work_dir = pdir / "output"
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        cfg.project_dir = pdir
        return cfg
    path = getattr(args, "config", None)
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"config not found: {p}")
        print(f"[cli] config: {p}")
        return config.load(p)
    return config.load()


def _resolve_source(args, cfg) -> str:
    """A bare filename resolves against the project's source/ folder."""
    src = args.source
    pdir = getattr(cfg, "project_dir", None)
    if pdir and not Path(src).exists():
        cand = pdir / "source" / src
        if cand.exists():
            return str(cand)
    return src


def cmd_doctor(args) -> int:
    from . import doctor
    return doctor.run(_load_cfg(args))


def cmd_candidates(args) -> int:
    cfg = _load_cfg(args)
    source = _resolve_source(args, cfg)
    job = _job_dir(cfg, source, args.name)
    brief = _load_brief(cfg, args.brief)
    _, _, cands = _pipeline_to_candidates(source, cfg, args.n, brief=brief)
    out = job / "candidates.json"
    out.write_text(json.dumps(cands, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_table(cands)
    print(f"[cli] wrote {out}")
    return 0


def cmd_run(args) -> int:
    cfg = _load_cfg(args)
    source = _resolve_source(args, cfg)
    job = _job_dir(cfg, source, args.name)
    fmt_names = [s for s in (args.formats or "").split(",") if s.strip()] or None
    formats = cfg.formats(fmt_names)
    brief = _load_brief(cfg, args.brief)

    if args.from_candidates:
        cands_path = job / "candidates.json"
        if not cands_path.exists():
            print(f"[cli] --from-candidates: no {cands_path}. "
                  f"Run `candidates` first.")
            return 1
        cands = json.loads(cands_path.read_text(encoding="utf-8"))
        if not cands:
            print(f"[cli] --from-candidates: {cands_path} is empty.")
            return 1
        # Reuse the picks already made and reviewed — skip model selection,
        # reaction and snapping. Order in the file is honored as-is (respects
        # hand-edits and re-ordering); only ingest + the cached transcript are
        # needed for rendering.
        from .ingest import ingest
        from .transcribe import transcribe
        video = ingest(source, cfg)
        print(f"[cli] source -> {video}")
        transcript = transcribe(video, cfg)
        # Defensive: hand-edited candidates.json can still contain overlaps.
        # Enforce the no-overlap house rule but preserve the file's order for
        # the clips that survive (from-candidates honors hand ordering).
        from .select import resolve_overlaps
        for i, c in enumerate(cands):
            c["_ord"] = i  # copied into resolved entries; restores hand order
        resolved = resolve_overlaps(cands, cfg.clip.get("min_seconds", 10))
        if len(resolved) < len(cands):
            print(f"[cli] overlap guard: dropped "
                  f"{len(cands) - len(resolved)} overlapping candidate(s) "
                  f"from {cands_path.name}")
        resolved.sort(key=lambda c: c.get("_ord", 1 << 30))
        for c in resolved:
            c.pop("_ord", None)
        cands = resolved
        print(f"[cli] from-candidates: {len(cands)} candidate(s) "
              f"from {cands_path.name}")
        _print_table(cands)
    else:
        video, transcript, cands = _pipeline_to_candidates(source, cfg, args.n,
                                                           brief=brief)
        _print_table(cands)
        (job / "candidates.json").write_text(
            json.dumps(cands, ensure_ascii=False, indent=2), encoding="utf-8")

    top = args.render_top or cfg.clip.get("render_top", 3)
    to_render = cands[:top]
    if not to_render:
        print("[cli] nothing to render.")
        return 1

    if getattr(args, "raw", False):
        return _export_raw(video, transcript, to_render, cfg, job)

    from .render import render_clip
    clips_dir = job / "clips"
    print(f"[cli] output: {job}  formats: {', '.join(f['name'] for f in formats)}")

    thumbs_on = cfg.raw.get("thumbnail", {}).get("enabled", False)
    make_thumbnail = None
    if thumbs_on:
        from .thumbnail import make_thumbnail
    title_thumbs_on = cfg.raw.get("thumbnail", {}).get("title_card", True)
    make_title_thumbnail = None
    if title_thumbs_on:
        from .thumbnail import make_title_thumbnail

    pdir = getattr(cfg, "project_dir", None) or job.parent
    keep_main = cfg.clip.get("keep_main_content", True)
    main_dir = pdir / "main_content"

    rendered = []
    for i, c in enumerate(to_render, 1):
        entry = {"clip": i, "outputs": {}, **c}
        for fmt in formats:
            out = clips_dir / f"clip_{i:02d}_{fmt['name']}.mp4"
            main_out = (main_dir / f"clip_{i:02d}_{fmt['name']}.mp4"
                        if keep_main else None)
            try:
                render_clip(video, c, transcript, cfg, out,
                            fmt["width"], fmt["height"], main_out=main_out)
                entry["outputs"][fmt["name"]] = str(out)
                if main_out and main_out.exists():
                    entry.setdefault("main_content", {})[fmt["name"]] = str(main_out)
            except Exception as exc:
                print(f"[cli] clip {i} [{fmt['name']}] failed: {exc}")
                continue
            if make_title_thumbnail:
                card = clips_dir / f"clip_{i:02d}_{fmt['name']}.title.png"
                thumb = clips_dir / f"clip_{i:02d}_{fmt['name']}.thumb.jpg"
                try:
                    make_title_thumbnail(video, c, cfg, thumb,
                                         fmt["width"], fmt["height"],
                                         card if card.exists() else None)
                    entry.setdefault("thumbnails", {})[fmt["name"]] = str(thumb)
                except Exception as exc:
                    print(f"[cli] clip {i} [{fmt['name']}] thumbnail failed: {exc}")
        if make_thumbnail:
            try:
                thumb = make_thumbnail(video, c, cfg, clips_dir / f"clip_{i:02d}_thumb.jpg")
                entry["thumbnail"] = str(thumb)
            except Exception as exc:
                print(f"[cli] clip {i} thumbnail failed: {exc}")
        # Per-clip editable transcript sidecars (clip-local, 0 = clip start):
        # .srt (canonical caption grid) + .txt + .words.json (per-word karaoke
        # timing). Edit either the .srt or the .words.json and `recut --clip i`
        # to re-render captions (recut prefers whichever was touched last).
        if entry["outputs"]:
            try:
                st, en = float(c["start"]), float(c["end"])
                _write_transcript_sidecars(
                    transcript, st, en,
                    clips_dir / f"clip_{i:02d}.srt",
                    clips_dir / f"clip_{i:02d}.txt")
                nw = _write_clip_words(
                    transcript, st, en, clips_dir / f"clip_{i:02d}.words.json")
                entry["transcript"] = {
                    "srt": str(clips_dir / f"clip_{i:02d}.srt"),
                    "words": str(clips_dir / f"clip_{i:02d}.words.json"),
                }
                if keep_main and (main_dir / f"clip_{i:02d}_{formats[0]['name']}.mp4").exists():
                    main_dir.mkdir(parents=True, exist_ok=True)
                    for ext in ("srt", "txt", "words.json"):
                        src = clips_dir / f"clip_{i:02d}.{ext}"
                        if src.exists():
                            shutil.copyfile(src, main_dir / f"clip_{i:02d}.{ext}")
                print(f"[cli] clip {i} sidecars: clip_{i:02d}.srt / "
                      f".txt / .words.json ({nw} words)")
            except Exception as exc:
                print(f"[cli] clip {i} transcript sidecar failed: {exc}")
        # Always also keep a full-frame raw cut of exactly what shipped, so every
        # project has raw_cuts/ for a later manual-focus pass. Toggle off per
        # project with [clip] keep_raw_cuts = false.
        if entry["outputs"] and cfg.clip.get("keep_raw_cuts", True):
            raw = _write_raw_cut(video, i, float(c["start"]), float(c["end"]),
                                 cfg, pdir / "raw_cuts")
            if raw:
                entry["raw"] = str(raw)
        if entry["outputs"]:
            rendered.append(entry)

    (clips_dir / "manifest.json").write_text(
        json.dumps(rendered, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(r["outputs"]) for r in rendered)
    print(f"\n[cli] rendered {total} files "
          f"({len(rendered)} clips x {len(formats)} formats) into {clips_dir}")
    for r in rendered:
        for name, path in r["outputs"].items():
            print(f"      [{name}] {path}")
    from . import tracker
    tracker.sync_safe(cfg)
    return 0 if rendered else 1


_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi")


def _find_job(cfg, name: str | None) -> Path:
    """Locate the run folder that holds clips/manifest.json."""
    if name:
        job = cfg.work_dir / _slug(name)
        if (job / "clips" / "manifest.json").exists():
            return job
    hits = list(cfg.work_dir.glob("*/clips/manifest.json"))
    if len(hits) == 1:
        return hits[0].parent.parent
    if not hits:
        raise FileNotFoundError(
            f"no clips/manifest.json under {cfg.work_dir}. Run `run` first.")
    names = ", ".join(sorted(h.parent.parent.name for h in hits))
    raise ValueError(f"multiple runs found ({names}); pass --name to pick one.")


def _load_cached_transcript(cfg) -> dict:
    """Load the transcript already cached for this run — never re-run whisper
    during a recut (that would risk changing the words the user is editing)."""
    tdir = cfg.work_dir / "transcripts"
    files = sorted(tdir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError(f"no cached transcript in {tdir}. Run `run` first.")
    return json.loads(files[0].read_text(encoding="utf-8"))


def _resolve_recut_source(args, cfg, job: Path) -> Path:
    """The source video for a recut: --source if given, else the sole video in
    the project's source/ folder, else the sole video anywhere under the run."""
    if getattr(args, "source", None):
        return Path(_resolve_source(args, cfg))
    pdir = getattr(cfg, "project_dir", None)
    search = []
    if pdir and (pdir / "source").is_dir():
        search.append(pdir / "source")
    for d in search:
        vids = [p for p in d.iterdir() if p.suffix.lower() in _VIDEO_EXTS]
        if len(vids) == 1:
            return vids[0]
        if len(vids) > 1:
            raise ValueError(
                f"multiple videos in {d}; pass --source to pick one.")
    raise FileNotFoundError(
        "could not find the source video; pass --source <file>.")


def cmd_recut(args) -> int:
    cfg = _load_cfg(args)
    job = _find_job(cfg, args.name)
    clips_dir = job / "clips"
    manifest_path = clips_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next((c for c in manifest if c.get("clip") == args.clip), None)
    if entry is None:
        nums = ", ".join(str(c.get("clip")) for c in manifest)
        print(f"[recut] no clip {args.clip} in {manifest_path} (have: {nums})")
        return 1

    transcript = _load_cached_transcript(cfg)
    words = transcript.get("words", [])
    start, end = float(entry["start"]), float(entry["end"])
    # Two editable clip-local formats: a friendly .srt (caption lines) and the
    # per-word .words.json. Both use clip-local time (0 = clip start). On re-run,
    # whichever the user edited most recently wins, so the two never fight.
    sidecar = clips_dir / f"clip_{args.clip:02d}.words.json"
    sidecar_srt = clips_dir / f"clip_{args.clip:02d}.srt"

    if getattr(args, "retranscribe", False):
        from .transcribe import transcribe_span
        video = _resolve_recut_source(args, cfg, job)
        words = transcribe_span(video, cfg, start, end)
        span_tx = {"language": "id", "words": words}
        _write_transcript_sidecars(span_tx, start, end, sidecar_srt,
                                   clips_dir / f"clip_{args.clip:02d}.txt")
        _write_clip_words(span_tx, start, end, sidecar)
        print(f"[recut] re-transcribed clip {args.clip} prompt-free; wrote fresh "
              f"{sidecar_srt.name} / {sidecar.name} — rendering.")
        # fall through to the render path below, which reads the fresh sidecar.

    if not sidecar.exists() and not sidecar_srt.exists():
        pad = 0.5
        slice_ = [
            {"start": round(w["start"] - start, 3), "end": round(w["end"] - start, 3),
             "word": w["word"]}
            for w in words
            if w.get("start") is not None
            and start - pad <= w["start"] <= end + pad
        ]
        sidecar.write_text(
            json.dumps(slice_, ensure_ascii=False, indent=2), encoding="utf-8")
        n_ev = _write_recut_srt(transcript, start, end, sidecar_srt)
        print(f"[recut] wrote editable transcript (clip-local 0.00-"
              f"{round(end - start, 2)}s):")
        print(f"        {sidecar_srt.name}  ({n_ev} caption lines) — easiest to edit")
        print(f"        {sidecar.name}  ({len(slice_)} words) — per-word timing")
        print("        Edit the caption text in EITHER file (remove Chinese / bad "
              "text), keep the timings, then re-run the SAME command to render.")
        return 0

    from .select import has_cjk
    # Prefer the editable file that was touched most recently.
    use_srt = sidecar_srt.exists() and (
        not sidecar.exists()
        or sidecar_srt.stat().st_mtime >= sidecar.stat().st_mtime)
    if use_srt:
        edited = _parse_recut_srt(sidecar_srt, start)
        src = sidecar_srt
    else:
        edited = [{**w, "start": round(w["start"] + start, 3),
                   "end": round(w["end"] + start, 3)}
                  for w in json.loads(sidecar.read_text(encoding="utf-8"))
                  if w.get("start") is not None]
        src = sidecar
    if any(has_cjk(w.get("word", "")) for w in edited):
        print(f"[recut] WARNING: {src.name} still contains Chinese/CJK text. "
              "Rendering anyway, but the captions will show it.")
    print(f"[recut] using edited transcript from {src.name}")
    edited_transcript = {
        "language": transcript.get("language"),
        "duration": transcript.get("duration"),
        "words": edited,
    }

    video = _resolve_recut_source(args, cfg, job)
    fmt_names = [s for s in (args.formats or "").split(",") if s.strip()] or None
    formats = cfg.formats(fmt_names)
    from .render import render_clip
    print(f"[recut] clip {args.clip} from {video.name}  "
          f"formats: {', '.join(f['name'] for f in formats)}")

    thumbs_on = cfg.raw.get("thumbnail", {}).get("enabled", False)
    pdir = getattr(cfg, "project_dir", None) or job.parent
    keep_main = cfg.clip.get("keep_main_content", True)
    main_dir = pdir / "main_content"
    ok = 0
    for fmt in formats:
        out = clips_dir / f"clip_{args.clip:02d}_{fmt['name']}.mp4"
        main_out = (main_dir / f"clip_{args.clip:02d}_{fmt['name']}.mp4"
                    if keep_main else None)
        try:
            render_clip(video, entry, edited_transcript, cfg, out,
                        fmt["width"], fmt["height"], main_out=main_out)
            entry.setdefault("outputs", {})[fmt["name"]] = str(out)
            if main_out and main_out.exists():
                entry.setdefault("main_content", {})[fmt["name"]] = str(main_out)
            ok += 1
        except Exception as exc:
            print(f"[recut] clip {args.clip} [{fmt['name']}] failed: {exc}")
            continue
        # Keep the vertical title-card thumbnail in sync with the (possibly new)
        # title: render_clip just rewrote the .title.png, so rebuild the thumb
        # from it. Without this a title change leaves a stale thumbnail.
        if thumbs_on:
            from .thumbnail import make_title_thumbnail
            card = clips_dir / f"clip_{args.clip:02d}_{fmt['name']}.title.png"
            tthumb = clips_dir / f"clip_{args.clip:02d}_{fmt['name']}.thumb.jpg"
            try:
                make_title_thumbnail(video, entry, cfg, tthumb,
                                     fmt["width"], fmt["height"],
                                     card if card.exists() else None)
                entry.setdefault("thumbnails", {})[fmt["name"]] = str(tthumb)
            except Exception as exc:
                print(f"[recut] clip {args.clip} [{fmt['name']}] "
                      f"title-thumb failed: {exc}")

    if ok and thumbs_on:
        from .thumbnail import make_thumbnail
        try:
            thumb = make_thumbnail(video, entry, cfg,
                                   clips_dir / f"clip_{args.clip:02d}_thumb.jpg")
            entry["thumbnail"] = str(thumb)
        except Exception as exc:
            print(f"[recut] clip {args.clip} thumbnail failed: {exc}")

    # Keep the full-frame raw cut in sync with the (possibly retimed) span.
    if ok and cfg.clip.get("keep_raw_cuts", True):
        raw = _write_raw_cut(video, args.clip, start, end, cfg,
                             pdir / "raw_cuts")
        if raw:
            entry["raw"] = str(raw)

    # Keep the Main Content transcript sidecars in sync with the recut edit.
    if ok and keep_main and main_dir.exists():
        for ext in ("srt", "txt", "words.json"):
            src = clips_dir / f"clip_{args.clip:02d}.{ext}"
            if src.exists():
                try:
                    shutil.copyfile(src, main_dir / f"clip_{args.clip:02d}.{ext}")
                except OSError:
                    pass

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[recut] re-rendered {ok} format(s) for clip {args.clip}.")
    return 0 if ok else 1


def cmd_thumbs(args) -> int:
    cfg = _load_cfg(args)
    job = _find_job(cfg, args.name)
    clips_dir = job / "clips"
    manifest_path = clips_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fmt = args.format or "mobile"
    dims = cfg.formats([fmt])[0]
    w, h = dims["width"], dims["height"]
    video = _resolve_recut_source(args, cfg, job)
    wanted = _parse_clip_range(args.clips)

    from .thumbnail import make_title_thumbnail
    print(f"[thumbs] {video.name}  format {fmt} ({w}x{h})")
    made = 0
    for c in manifest:
        num = c.get("clip")
        if wanted is not None and num not in wanted:
            continue
        card = clips_dir / f"clip_{num:02d}_{fmt}.title.png"
        out = clips_dir / f"clip_{num:02d}_{fmt}.thumb.jpg"
        try:
            make_title_thumbnail(video, c, cfg, out, w, h,
                                 card if card.exists() else None)
            c.setdefault("thumbnails", {})[fmt] = str(out)
            made += 1
        except Exception as exc:
            print(f"[thumbs] clip {num} failed: {exc}")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[thumbs] wrote {made} thumbnail(s) into {clips_dir}")
    return 0 if made else 1


def _read_srt_text(srt_path: Path, max_chars: int = 600) -> str:
    """Join a clip-local .srt's caption lines into one trimmed paragraph. Reads
    only the small per-clip SRT — never the source transcript or words.json."""
    if not srt_path.exists():
        return ""
    lines: list[str] = []
    for block in re.split(r"\n\s*\n", srt_path.read_text(encoding="utf-8").strip()):
        rows = [ln.strip() for ln in block.splitlines() if ln.strip()]
        text = " ".join(r for r in rows if "-->" not in r and not r.isdigit())
        if text:
            lines.append(text)
    joined = " ".join(lines).strip()
    if len(joined) > max_chars:
        joined = joined[:max_chars].rstrip() + " …"
    return joined


def cmd_review(args) -> int:
    """Compact, token-cheap digest of a whole run: per clip its score, times,
    title, hook, why, and what's actually said (from the small clip-local .srt).
    Everything needed to review timing, spot caption errors and author titles —
    WITHOUT opening the 500KB+ source transcript or the per-clip words.json.
    Prints a few KB total; use this instead of Read-ing those files."""
    cfg = _load_cfg(args)
    job = _find_job(cfg, args.name)
    clips_dir = job / "clips"
    manifest_path = clips_dir / "manifest.json"
    if manifest_path.exists():
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        src_label = "manifest.json"
    else:
        cands_path = job / "candidates.json"
        if not cands_path.exists():
            print(f"[review] no manifest.json or candidates.json under {job}. "
                  f"Run `run` first.")
            return 1
        entries = json.loads(cands_path.read_text(encoding="utf-8"))
        src_label = "candidates.json (not yet rendered)"

    wanted = _parse_clip_range(args.clips)
    pdir = getattr(cfg, "project_dir", None)

    print(f"[review] {job.name}  ({len(entries)} clips, from {src_label})")
    for fname in ("People.txt", "Words.txt"):
        if pdir and (pdir / fname).exists():
            roster = " ".join((pdir / fname).read_text(encoding="utf-8").split())
            if roster:
                print(f"  {fname[:-4]}: {roster[:300]}")

    shown = 0
    for i, c in enumerate(entries, 1):
        num = c.get("clip", i)
        if wanted is not None and num not in wanted:
            continue
        start = float(c.get("start", 0.0))
        end = float(c.get("end", 0.0))
        length = c.get("duration", end - start)
        title = (c.get("title") or "").strip() or "(no title)"
        hook = (c.get("hook") or "").replace("\n", " ").strip()
        why = (c.get("why") or "").replace("\n", " ").strip()
        print()
        print(f"-- clip {num:>2}  score {c.get('score', 0):.1f}  "
              f"{_mmss(start)}-{_mmss(end)} ({length:.1f}s)")
        print(f"   title: {title}")
        if hook:
            print(f"   hook : {hook[:140]}")
        if why:
            print(f"   why  : {why[:120]}")
        if c.get("risks"):
            print(f"   risk : {'; '.join(c['risks'])[:120]}")
        caption = _read_srt_text(clips_dir / f"clip_{num:02d}.srt")
        if not caption:
            txt = clips_dir / f"clip_{num:02d}.txt"
            if txt.exists():
                caption = " ".join(txt.read_text(encoding="utf-8").split())[:600]
        print(f"   said : {caption or '(no .srt yet - run/recut to generate captions)'}")
        shown += 1

    if wanted is not None and shown == 0:
        print(f"\n[review] no clips matched {args.clips}.")
        return 1
    print()
    return 0


_PROJECT_CONFIG_TEMPLATE = '''# Project: {name}
# Overrides on top of the base config.toml — list only what differs.

[title]
prefix = ""                 # e.g. "Live Take Profit Show Suli :"

[watermark]
text = "@ojantigakali"

[outro]
enabled = true
text = "Nonton selengkapnya di YouTube: {channel}"

[clip]
fit = "blur"                # crop | contain | blur

[transcribe]
# High-coverage decoding: loosen the VAD + no-speech gates so quiet, over-laughter
# and cross-talk speech isn't silently dropped. On by default for new projects;
# the run prints a coverage % + a list of GAP spans so you can see what (if
# anything) got missed. Set false to fall back to the tighter base defaults.
coverage_mode = true

[upload]
title_prefix = ""
hashtags = ["#Shorts"]
description_footer = ""
'''

_PROJECT_BRIEF_TEMPLATE = '''DO: <the kinds of moments to clip>
DON'T: <what to avoid — intros, ads, etc.>
TOPIC FOCUS: <optional>
TONE: <optional>
'''


def cmd_init(args) -> int:
    channel = (getattr(args, "youtube", None) or "").strip()
    if not channel:
        try:
            channel = input(
                'YouTube channel for the outro CTA (e.g. "@ojantigakali"): ').strip()
        except EOFError:
            channel = ""
    if not channel:
        print('[init] aborted: an outro YouTube channel is required — '
              'pass --youtube "@handle"')
        return 2
    if not channel.startswith("@") and "/" not in channel and " " not in channel:
        channel = "@" + channel

    pdir = _project_dir(args.name)
    (pdir / "source").mkdir(parents=True, exist_ok=True)
    (pdir / "output").mkdir(parents=True, exist_ok=True)
    cfgp = pdir / "config.toml"
    if not cfgp.exists():
        cfgp.write_text(
            _PROJECT_CONFIG_TEMPLATE.format(name=args.name, channel=channel),
            encoding="utf-8")
    briefp = pdir / "brief.txt"
    if not briefp.exists():
        briefp.write_text(_PROJECT_BRIEF_TEMPLATE, encoding="utf-8")
    print(f"[init] project ready: {pdir}")
    print( "        config.toml   — branding/settings (edit me)")
    print( "        brief.txt     — do/don't guidance (edit me)")
    print( "        source/       — drop your video files here")
    print( "        output/       — clips, thumbnails, manifest land here")
    print(f'\nNext:  py -3 -m clipcraft run --project "{args.name}" --source <file-in-source/> --render-top 20')
    return 0


def _parse_clip_range(spec: str | None) -> set[int] | None:
    """Parse a clip selector like "1-10", "1,3,5", or "1-5,8" into a set of ints.

    Returns None when no selector is given (meaning: upload every clip).
    """
    if not spec:
        return None
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return out


def _mmss(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 60:02d}m{t % 60:02d}s"


def _srt_time(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_transcript_sidecars(transcript: dict, start: float, end: float,
                               srt_path: Path, txt_path: Path) -> None:
    """Write a clip-local .srt (subtitle grid) and .txt (plain transcript) for
    the window [start, end]. Timings are relative to the clip start."""
    from .render import _group_words
    words = transcript.get("words", [])
    events = _group_words(words, start, end, max_words=8, max_chars=42)
    lines = []
    for i, ev in enumerate(events, 1):
        lines.append(f"{i}\n{_srt_time(ev['start'])} --> {_srt_time(ev['end'])}\n"
                     f"{ev['text']}\n")
    srt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    txt_path.write_text(" ".join(e["text"] for e in events).strip() + "\n",
                        encoding="utf-8")


def _write_clip_words(transcript: dict, start: float, end: float,
                      path: Path) -> int:
    """Write a clip-local per-word .words.json (karaoke timing source, editable
    for recut) for the window [start, end]. Timings are relative to the clip
    start (0 = clip start); recut shifts them back to source time internally."""
    pad = 0.5
    words = transcript.get("words", [])
    slice_ = [
        {"start": round(w["start"] - start, 3),
         "end": round(w["end"] - start, 3),
         "word": w["word"]}
        for w in words
        if w.get("start") is not None and start - pad <= w["start"] <= end + pad
    ]
    path.write_text(json.dumps(slice_, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return len(slice_)


_SRT_TIMING_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def _write_recut_srt(transcript: dict, start: float, end: float,
                     srt_path: Path) -> int:
    """Editable clip-local .srt for a recut: grouped caption lines with timings
    relative to the clip start (0 = clip start), so the file is edited against the
    rendered clip's own timeline, not the multi-thousand-second source position.
    Returns the number of caption events written."""
    from .render import _group_words
    events = _group_words(transcript.get("words", []), start, end,
                          max_words=8, max_chars=42)
    lines = [f"{i}\n{_srt_time(ev['start'])} --> {_srt_time(ev['end'])}\n"
             f"{ev['text']}\n" for i, ev in enumerate(events, 1)]
    srt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(events)


def _parse_recut_srt(srt_path: Path, start: float) -> list[dict]:
    """Parse an edited clip-local .srt back into SOURCE-time pseudo-words. Each
    caption line's [s,e] window is split evenly across its tokens, then shifted
    by the clip's `start` offset (render works in source time). Karaoke per-word
    timing is approximate, but the edited text is preserved exactly."""
    words: list[dict] = []
    text = srt_path.read_text(encoding="utf-8")
    for block in re.split(r"\n\s*\n", text.strip()):
        rows = [ln for ln in block.splitlines() if ln.strip()]
        tline = next((ln for ln in rows if "-->" in ln), None)
        if not tline:
            continue
        m = _SRT_TIMING_RE.search(tline)
        if not m:
            continue
        caption = " ".join(rows[rows.index(tline) + 1:]).strip()
        toks = caption.split()
        if not toks:
            continue
        g = [int(x) for x in m.groups()]
        s = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        per = max(0.05, e - s) / len(toks)
        for i, tok in enumerate(toks):
            words.append({"start": round(s + i * per + start, 3),
                          "end": round(s + (i + 1) * per + start, 3),
                          "word": tok})
    return words


def _write_raw_cut(video: Path, i: int, start: float, end: float, cfg,
                   raw_dir: Path) -> Path | None:
    """Full-frame (un-reframed) source cut of one clip's content span into
    raw_cuts/ — the same encode as the `run --raw` manual-focus export, but
    written alongside a normal reframe render so every project always keeps a
    raw cut of exactly what shipped. Any older span-named raw cut for this clip
    number is removed first, so a retimed clip never leaves a stale file behind.
    Returns the output path, or None on failure."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    stem = f"clip_{i:02d}"
    for old in raw_dir.glob(f"{stem}_raw_*.mp4"):
        try:
            old.unlink()
        except OSError:
            pass
    raw_out = raw_dir / f"{stem}_raw_{_mmss(start)}-{_mmss(end)}.mp4"
    cmd = [cfg.ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", str(video),
           "-t", f"{round(end - start, 3):.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", str(raw_out)]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        print(f"[raw] clip {i} cut failed:\n"
              + "\n".join(proc.stdout.splitlines()[-12:]))
        return None
    print(f"[raw] clip {i} -> raw_cuts/{raw_out.name}  "
          f"({start:.2f}-{end:.2f}, {end - start:.1f}s)")
    return raw_out


def _export_raw(video: Path, transcript: dict, clips: list[dict], cfg,
                job: Path) -> int:
    """Raw-cut export: for each picked clip, write a full-frame (un-reframed) cut
    to <project>/raw_cuts/, plus its title-card thumbnail, TTS hook voiceover,
    and transcript sidecars — everything the user needs to do the manual focus
    pass, and everything `finish` reads back. The manifest (in the run folder) is
    the single source of truth for `finish`/`upload`/`tiktok`/`telegram`."""
    pdir = getattr(cfg, "project_dir", None) or job.parent
    raw_dir = pdir / "raw_cuts"
    raw_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = job / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    from . import tts
    from .thumbnail import make_title_thumbnail
    fmt = cfg.formats()[0]  # thumbnail geometry (vertical)
    ttscfg = cfg.raw.get("tts", {})
    tts_on = ttscfg.get("enabled", False) and tts.available()

    rendered = []
    for i, c in enumerate(clips, 1):
        start, end = float(c["start"]), float(c["end"])
        stem = f"clip_{i:02d}"
        raw_out = _write_raw_cut(video, i, start, end, cfg, raw_dir)
        if raw_out is None:
            continue

        entry = {"clip": i, "outputs": {}, "raw": str(raw_out), **c}

        thumb = raw_dir / f"{stem}.thumb.jpg"
        try:
            make_title_thumbnail(video, c, cfg, thumb, fmt["width"], fmt["height"])
            entry.setdefault("thumbnails", {})[fmt["name"]] = str(thumb)
        except Exception as exc:
            print(f"[raw] clip {i} thumbnail failed: {exc}")

        srt, txt = raw_dir / f"{stem}.srt", raw_dir / f"{stem}.txt"
        try:
            _write_transcript_sidecars(transcript, start, end, srt, txt)
            entry["transcript"] = {"srt": str(srt), "txt": str(txt)}
        except Exception as exc:
            print(f"[raw] clip {i} transcript sidecar failed: {exc}")

        if tts_on:
            vtext = tts._voice_text(c, ttscfg.get("source", "hook"))
            voice = raw_dir / f"{stem}.voice.mp3"
            if vtext and tts.synthesize(vtext, voice, cfg) is not None:
                entry["tts"] = str(voice)
                print(f"[raw] clip {i} voiceover -> {voice.name}")

        rendered.append(entry)

    (clips_dir / "manifest.json").write_text(
        json.dumps(rendered, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[raw] {len(rendered)} raw cut(s) in {raw_dir}")
    print(f"[raw] manifest: {clips_dir / 'manifest.json'}")
    print("[raw] next: do your focus pass on each raw cut, then\n"
          '      py -3 -m clipcraft finish --project "<name>" --clip N '
          '--file "<your_focused_clip.mp4>"')
    return 0 if rendered else 1


def cmd_finish(args) -> int:
    """Re-render a user-framed ('focused') clip with hook TTS, captions, title
    card and outro — no reframe. Two modes:

    * manifest mode — a `run --raw` manifest holds the title/hook/transcript for
      `--clip N`; captions auto-align (reuse the master transcript when the focused
      clip's length matches the raw cut, else re-transcribe the file).
    * standalone mode — no manifest (or no matching entry): finish any edited clip
      on its own. Title/hook come from `--title` / `--hook` (`--no-hook` to skip),
      and the file is always transcribed for captions. Works with no prior run."""
    cfg = _load_cfg(args)
    src = Path(args.file)
    if not src.exists():
        print(f"[finish] file not found: {src}")
        return 1

    try:
        job = _find_job(cfg, args.name)
        manifest_path = job / "clips" / "manifest.json"
        clips = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        job, clips = None, []
    entry = next((c for c in clips if c.get("clip") == args.clip), None)

    standalone = entry is None
    if standalone:
        job = _job_dir(cfg, str(src), args.name)
        manifest_path = job / "clips" / "manifest.json"
        entry = {"clip": args.clip}
        print(f"[finish] standalone mode: no manifest entry for clip "
              f"{args.clip} — transcribing your clip for captions.")

    # Title/hook overrides (required in standalone; optional in manifest mode).
    if getattr(args, "title", None):
        entry["title"] = args.title
    if getattr(args, "no_hook", False):
        entry["hook"] = ""
        entry.pop("tts", None)  # drop any pre-synth voice so render_finish skips it
    elif getattr(args, "hook", None):
        entry["hook"] = args.hook
        entry.pop("tts", None)  # force re-synth of the overridden hook text

    fmt = cfg.formats(([s for s in (args.formats or '').split(',') if s.strip()]
                       or None))[0]
    from . import tts
    from .render import render_finish

    in_dur = tts._probe_duration(cfg.ffprobe, src)
    can_reuse = (not standalone) and "start" in entry and "end" in entry
    if can_reuse:
        master_dur = round(float(entry["end"]) - float(entry["start"]), 3)
        tol = float(getattr(args, "align_tol", 0.75) or 0.75)
        reuse = in_dur > 0 and abs(in_dur - master_dur) <= tol
    else:
        reuse = False

    if reuse:
        # master transcript is cached from the raw run; reuse its word grid.
        transcript = _load_cached_transcript(cfg)
        words = transcript.get("words", [])
        win_start, win_end = float(entry["start"]), float(entry["end"])
        print(f"[finish] clip {args.clip}: length matches raw cut "
              f"({in_dur:.1f}s≈{master_dur:.1f}s) — reusing master transcript.")
    else:
        from .transcribe import transcribe as _tx
        why = ("standalone" if standalone
               else f"length differs ({in_dur:.1f}s vs raw {master_dur:.1f}s)")
        print(f"[finish] clip {args.clip}: {why} — transcribing your clip.")
        transcript = _tx(src, cfg)
        words = transcript.get("words", [])
        win_start, win_end = 0.0, round(in_dur, 3) if in_dur > 0 else 0.0

    out = job / "clips" / f"clip_{args.clip:02d}_{fmt['name']}.mp4"
    render_finish(src, entry, cfg, out, fmt["width"], fmt["height"],
                  words, win_start, win_end)
    entry.setdefault("outputs", {})[fmt["name"]] = str(out)
    entry["finished_from"] = str(src)
    if entry not in clips:
        clips.append(entry)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(clips, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(f"[finish] clip {args.clip} -> {out}")
    print(f"[finish] manifest updated. Upload with:\n"
          f'      py -3 -m clipcraft upload --project "{args.project}" '
          f'--name "{job.name}" --clips {args.clip}')
    return 0


def cmd_core(args) -> int:
    """Assemble a CORE (montage of iconic moments) from a manifest."""
    cfg = _load_cfg(args)
    from . import core as corelib

    pdir = getattr(cfg, "project_dir", None)
    if args.manifest:
        manifest_path = Path(args.manifest)
        if not manifest_path.exists() and pdir:
            manifest_path = pdir / args.manifest
    elif pdir:
        manifest_path = pdir / "core.json"
    else:
        print("[core] pass --manifest <core.json> (or --project with a core.json).")
        return 1
    if not manifest_path.exists():
        print(f"[core] manifest not found: {manifest_path}")
        return 1

    manifest = corelib.load_manifest(manifest_path)
    fmt = cfg.formats([args.format] if args.format else None)[0]

    if args.out:
        out_path = Path(args.out)
    else:
        out_dir = (pdir / "output") if pdir else cfg.work_dir
        stem = _slug(manifest.get("label", "core")) or "core"
        out_path = out_dir / f"{stem}_core.mp4"

    corelib.build_core(manifest, cfg, out_path, fmt["width"], fmt["height"],
                       project_dir=pdir, manifest_dir=manifest_path.parent)
    print(f"\n[core] output: {out_path}")
    return 0


def cmd_upload(args) -> int:
    cfg = _load_cfg(args)
    job = _find_job(cfg, args.name)
    manifest_path = job / "clips" / "manifest.json"
    if not manifest_path.exists():
        print(f"[upload] no manifest at {manifest_path}. Run `clipcraft run` first.")
        return 1
    clips = json.loads(manifest_path.read_text(encoding="utf-8"))

    from . import youtube
    acc_name = youtube._account_cfg(cfg, args.account)["name"]

    wanted = _parse_clip_range(args.clips)
    jobs = []
    for c in clips:
        if wanted is not None and c.get("clip") not in wanted:
            continue
        gate = _review_gate(c, cfg, args.force)
        if gate:
            print(f"[upload] clip {c.get('clip')} blocked by review gate: {gate}; "
                  "skipping (rate it, or use --force)")
            continue
        prior = (c.get("uploads") or {}).get(acc_name)
        if not args.force and prior:
            print(f"[upload] clip {c.get('clip')} already uploaded to '{acc_name}' "
                  f"({prior.get('url')}), skipping (use --force to re-upload)")
            continue
        path = (c.get("outputs") or {}).get(args.format)
        if path and Path(path).exists():
            jobs.append((c, path))
        else:
            print(f"[upload] clip {c.get('clip')} has no '{args.format}' file, skipping")
    if not jobs:
        print(f"[upload] nothing to upload for format '{args.format}'.")
        return 1

    return youtube.upload_batch(jobs, cfg, manifest_path, clips,
                                privacy=args.privacy, spacing=args.every,
                                dry_run=args.dry_run, account=args.account)


def cmd_tiktok(args) -> int:
    cfg = _load_cfg(args)
    from . import tiktok
    if args.auth:
        url = tiktok.build_auth_url(cfg)
        print("[tiktok] 1) Open this URL, log in, and approve:\n")
        print(f"  {url}\n")
        print("[tiktok] 2) TikTok redirects to your https redirect_uri with `?code=`"
              " in the address bar (the page may fail to load — that's fine).\n"
              "   Copy the code (or the whole redirected URL) and run:\n"
              "   py -3 -m clipcraft tiktok --project <name> --code \"<paste-here>\"")
        return 0
    if args.code:
        tiktok.complete_auth(cfg, args.code)
        print("[tiktok] authorized. Token cached to .tiktok_token.json. Now run "
              "`tiktok` (no flags) to push the clips.")
        return 0
    job = _find_job(cfg, args.name)
    manifest_path = job / "clips" / "manifest.json"
    if not manifest_path.exists():
        print(f"[tiktok] no manifest at {manifest_path}. Run `run` first.")
        return 1
    clips = json.loads(manifest_path.read_text(encoding="utf-8"))
    wanted = _parse_clip_range(args.clips)
    jobs = []
    for c in clips:
        if wanted is not None and c.get("clip") not in wanted:
            continue
        gate = _review_gate(c, cfg, args.force)
        if gate:
            print(f"[tiktok] clip {c.get('clip')} blocked by review gate: {gate}; "
                  "skipping (rate it, or use --force)")
            continue
        if not args.force and (c.get("tiktok") or {}).get("status") == "inbox":
            print(f"[tiktok] clip {c.get('clip')} already pushed to inbox, skipping "
                  "(use --force to re-upload)")
            continue
        path = (c.get("outputs") or {}).get(args.format)
        if path and Path(path).exists():
            jobs.append((c, path))
        else:
            print(f"[tiktok] clip {c.get('clip')} has no '{args.format}' file, skipping")
    if not jobs:
        print(f"[tiktok] nothing to upload for format '{args.format}'.")
        return 1
    from . import tiktok
    return tiktok.upload_batch(jobs, cfg, manifest_path, clips, dry_run=args.dry_run)


def cmd_telegram(args) -> int:
    cfg = _load_cfg(args)
    from . import telegram
    if args.whoami:
        return telegram.whoami(cfg)
    job = _find_job(cfg, args.name)
    manifest_path = job / "clips" / "manifest.json"
    if not manifest_path.exists():
        print(f"[telegram] no manifest at {manifest_path}. Run `run` first.")
        return 1
    clips = json.loads(manifest_path.read_text(encoding="utf-8"))
    wanted = _parse_clip_range(args.clips)
    jobs = []
    for c in clips:
        if wanted is not None and c.get("clip") not in wanted:
            continue
        path = (c.get("outputs") or {}).get(args.format)
        if path and Path(path).exists():
            jobs.append((c, path))
        else:
            print(f"[telegram] clip {c.get('clip')} has no '{args.format}' file, skipping")
    if not jobs:
        print(f"[telegram] nothing to send for format '{args.format}'.")
        return 1
    return telegram.send_batch(jobs, cfg, manifest_path, clips, dry_run=args.dry_run,
                               channel=getattr(args, "channel", None))


def _me(args) -> str:
    """Who is running this — --by wins, else env CLIPCRAFT_USER."""
    import os
    return (getattr(args, "by", None) or os.environ.get("CLIPCRAFT_USER", "")).strip()


def _load_manifest(cfg, name):
    job = _find_job(cfg, name)
    mp = job / "clips" / "manifest.json"
    if not mp.exists():
        raise FileNotFoundError(f"no manifest at {mp}. Run `run` first.")
    return mp, json.loads(mp.read_text(encoding="utf-8"))


def _review_gate(clip, cfg, force: bool) -> str | None:
    """Return a skip-reason if publishing this clip is blocked by the review gate,
    else None. Opt-in via [review] require_approval; --force overrides. Gates the
    auto-publishers (YouTube/TikTok) only — never the Telegram review delivery."""
    if force or not cfg.raw.get("review", {}).get("require_approval", False):
        return None
    status = (clip.get("status") or "").strip().lower()
    if status != "approved":
        return f"not approved (status={status or 'unset'})"
    return None


def cmd_claim(args) -> int:
    """Mark clips as owned by a person (the producer). Sets owner + a draft status
    so the reviewer is forced to be someone else (four-eyes)."""
    cfg = _load_cfg(args)
    who = _me(args)
    if not who:
        print("[claim] who are you? pass --by <name> or set CLIPCRAFT_USER.")
        return 1
    mp, clips = _load_manifest(cfg, args.name)
    wanted = _parse_clip_range(args.clips)
    n = 0
    for c in clips:
        if wanted is not None and c.get("clip") not in wanted:
            continue
        c["owner"] = who
        if not c.get("status"):
            c["status"] = "draft"
        n += 1
    mp.write_text(json.dumps(clips, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[claim] {n} clip(s) -> owner={who}")
    from . import tracker
    tracker.sync_safe(cfg)
    return 0


def cmd_rate(args) -> int:
    """Reviewer verdict on clips: pass (-> approved) or redo (-> changes), with an
    optional 1-5 score and note. Enforces four-eyes: you can't rate your own clip."""
    cfg = _load_cfg(args)
    who = _me(args)
    if not who:
        print("[rate] who are you? pass --by <name> or set CLIPCRAFT_USER.")
        return 1
    verdict = "redo" if args.redo else "pass"
    if args.score is not None and not (1 <= args.score <= 5):
        print("[rate] --score must be 1-5.")
        return 1
    mp, clips = _load_manifest(cfg, args.name)
    wanted = _parse_clip_range(args.clips)
    if wanted is None:
        print("[rate] name the clip(s) with --clips (e.g. --clips 1 or 1,3,5).")
        return 1
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    n = 0
    for c in clips:
        if c.get("clip") not in wanted:
            continue
        owner = (c.get("owner") or "").strip()
        if owner and owner.lower() == who.lower() and not args.force:
            print(f"[rate] clip {c.get('clip')}: four-eyes — you (={who}) own this "
                  f"clip; someone else must rate it (or use --force).")
            continue
        c["status"] = "approved" if verdict == "pass" else "changes"
        c["review"] = {"by": who, "verdict": verdict, "score": args.score,
                       "note": (args.note or "").strip(), "at": now}
        n += 1
        print(f"[rate] clip {c.get('clip')} -> {c['status']}"
              + (f" ({args.score}/5)" if args.score else "")
              + (f"  \"{args.note}\"" if args.note else ""))
    if n:
        mp.write_text(json.dumps(clips, ensure_ascii=False, indent=2), encoding="utf-8")
        from . import tracker
        tracker.sync_safe(cfg)
    return 0 if n else 1


def cmd_track(args) -> int:
    cfg = _load_cfg(args)
    from . import tracker
    clip_rows, camp_rows = tracker.collect(cfg.root)
    print(f"[track] {len(clip_rows) - 1} clip(s) across {len(camp_rows) - 1} "
          f"campaign(s):")
    for row in camp_rows:
        print("  " + " | ".join(str(x) for x in row))
    if args.dry_run:
        print("[track] dry-run: not synced to the sheet.")
        return 0
    n = tracker.sync(cfg)
    print(f"[track] synced {n} clip(s) to the Google Sheet.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clipcraft",
                                description="Local-first video clipping.")
    p.add_argument("--version", action="version", version=f"clipcraft {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=None,
                        help='project folder under projects/ (self-contained: '
                             'config, brief, source/, output/)')
    common.add_argument("--config", default=None,
                        help="path to a config.toml override (ignored if --project set)")

    sub = p.add_subparsers(dest="command", required=True)

    ip = sub.add_parser("init", help="scaffold a new project folder")
    ip.add_argument("name", help='project name, e.g. "Suli Crypto Live"')
    ip.add_argument("--youtube", "--outro-channel", dest="youtube", default=None,
                    metavar="@HANDLE",
                    help='YouTube channel for the required outro CTA '
                         '("Nonton selengkapnya di YouTube: <handle>"). '
                         'Prompted if omitted.')
    ip.set_defaults(func=cmd_init)

    d = sub.add_parser("doctor", help="check environment", parents=[common])
    d.set_defaults(func=cmd_doctor)

    c = sub.add_parser("candidates", help="list clip candidates (no rendering)",
                       parents=[common])
    c.add_argument("--source", required=True, help="video URL or local file path")
    c.add_argument("--n", type=int, default=10, help="how many candidates")
    c.add_argument("--name", default=None,
                   help="output folder name under work/ (default: from source)")
    c.add_argument("--brief", default=None,
                   help="path to a do/don't brief file, or inline brief text")
    c.set_defaults(func=cmd_candidates)

    r = sub.add_parser("run", help="pick candidates and render clips", parents=[common])
    r.add_argument("--source", required=True, help="video URL or local file path")
    r.add_argument("--n", type=int, default=10, help="how many candidates to pick")
    r.add_argument("--render-top", type=int, default=0,
                   help="how many top clips to render (default from config)")
    r.add_argument("--name", default=None,
                   help="output folder name under work/ (default: from source)")
    r.add_argument("--formats", default=None,
                   help="comma-separated formats to render, e.g. mobile,desktop "
                        "(default from config)")
    r.add_argument("--brief", default=None,
                   help="path to a do/don't brief file, or inline brief text")
    r.add_argument("--from-candidates", action="store_true",
                   help="render straight from the existing candidates.json "
                        "(skip model selection; honors file order and hand-edits)")
    r.add_argument("--raw", action="store_true",
                   help="export un-reframed full-frame RAW cuts (+ title, thumbnail, "
                        "TTS, transcript) into <project>/raw_cuts/ for a manual focus "
                        "pass; finish them later with `finish`")
    r.set_defaults(func=cmd_run)

    fn = sub.add_parser("finish",
                        help="re-render a hand-framed clip with hook TTS, captions, "
                             "title card and outro (no reframe). Works from a "
                             "`run --raw` manifest, or standalone with --title/--hook",
                        parents=[common])
    fn.add_argument("--clip", type=int, default=1,
                    help="clip number from the raw run's manifest; in standalone "
                         "mode it just names the output (default 1)")
    fn.add_argument("--file", required=True,
                    help="your focused/reframed clip file")
    fn.add_argument("--title", default=None,
                    help="title card text; overrides the manifest, required for a "
                         "titled standalone finish")
    fn.add_argument("--hook", default=None,
                    help="TTS hook text; overrides the manifest (standalone mode)")
    fn.add_argument("--no-hook", action="store_true",
                    help="skip the TTS hook voiceover entirely")
    fn.add_argument("--name", default=None,
                    help="run folder under output/ (auto-detected if only one)")
    fn.add_argument("--formats", default=None,
                    help="comma-separated formats to render (default from config)")
    fn.add_argument("--align-tol", type=float, default=0.75,
                    help="seconds of length difference within which the master "
                         "transcript is reused instead of re-transcribing (default 0.75)")
    fn.set_defaults(func=cmd_finish)

    rc = sub.add_parser("recut", help="re-render one clip from an edited transcript",
                        parents=[common])
    rc.add_argument("--clip", type=int, required=True, help="clip number (from manifest)")
    rc.add_argument("--name", default=None,
                    help="run folder under output/ (auto-detected if only one)")
    rc.add_argument("--source", default=None,
                    help="source video (auto-detected from the project source/ folder)")
    rc.add_argument("--formats", default=None,
                    help="comma-separated formats to render (default from config)")
    rc.add_argument("--retranscribe", action="store_true",
                    help="re-decode this clip's audio prompt-free / hotword-free "
                         "(fixes Words.txt echoes, silence hallucinations and "
                         "dropped code-switched speech) before rendering")
    rc.set_defaults(func=cmd_recut)

    th = sub.add_parser("thumbs",
                        help="make 9:16 title-card thumbnails (sharp face frame + "
                             "the clip's yellow title card)",
                        parents=[common])
    th.add_argument("--name", default=None,
                    help="run folder under output/ (auto-detected if only one)")
    th.add_argument("--format", default="mobile",
                    help="which rendered format to thumbnail (default: mobile)")
    th.add_argument("--source", default=None,
                    help="source video (auto-detected from the project source/ folder)")
    th.add_argument("--clips", default=None,
                    help="only these clip numbers, e.g. '1-10' or '1,3,5' "
                         "(default: all clips in the manifest)")
    th.set_defaults(func=cmd_thumbs)

    rv = sub.add_parser("review",
                        help="compact, token-cheap digest of a run (scores, times, "
                             "titles, hooks, captions) — review a run without "
                             "opening the big transcript or words.json",
                        parents=[common])
    rv.add_argument("--name", default=None,
                    help="run folder under output/ (auto-detected if only one)")
    rv.add_argument("--clips", default=None,
                    help="only these clip numbers, e.g. '1-5' or '1,3,5' "
                         "(default: all clips)")
    rv.set_defaults(func=cmd_review)

    co = sub.add_parser("core",
                        help="assemble a CORE montage from a scene manifest",
                        parents=[common])
    co.add_argument("--manifest", default=None,
                    help="path to a core.json manifest (default: <project>/core.json)")
    co.add_argument("--format", default=None,
                    help="output format from [formats.*] (default: first in config)")
    co.add_argument("--out", default=None,
                    help="output file path (default: <project>/output/<label>_core.mp4)")
    co.set_defaults(func=cmd_core)

    u = sub.add_parser("upload", help="bulk-upload a run's clips to YouTube (Shorts)",
                       parents=[common])
    u.add_argument("--name", required=True, help="the run folder under work/ to upload")
    u.add_argument("--format", default="mobile",
                   help="which rendered format to upload (default: mobile)")
    u.add_argument("--privacy", default="unlisted",
                   choices=["private", "unlisted", "public"],
                   help="YouTube visibility (default: unlisted)")
    u.add_argument("--every", type=int, default=60,
                   help="seconds to wait between uploads (anti-spam, default 60)")
    u.add_argument("--account", default=None,
                   help="named upload account from [upload.accounts.*] "
                        "(default: upload.default_account, else 'main')")
    u.add_argument("--clips", default=None,
                   help="only upload these clip numbers, e.g. '1-10' or '1,3,5' "
                        "(default: all clips in the manifest)")
    u.add_argument("--force", action="store_true",
                   help="re-upload even clips already marked uploaded in the manifest")
    u.add_argument("--dry-run", action="store_true",
                   help="show what would upload without calling YouTube")
    u.set_defaults(func=cmd_upload)

    tk = sub.add_parser("tiktok", help="push a run's clips to TikTok inbox as drafts",
                        parents=[common])
    tk.add_argument("--name", default=None,
                    help="run folder under output/ (auto-detected if only one)")
    tk.add_argument("--format", default="mobile",
                    help="which rendered format to upload (default: mobile)")
    tk.add_argument("--auth", action="store_true",
                    help="step 1: print the TikTok authorize URL")
    tk.add_argument("--code", default=None,
                    help="step 2: the code (or full redirected URL) from --auth")
    tk.add_argument("--clips", default=None,
                    help="only push these clip numbers, e.g. '7-10' or '1,3,5' "
                         "(default: all clips not already in the inbox)")
    tk.add_argument("--force", action="store_true",
                    help="re-upload even clips already pushed to the inbox")
    tk.add_argument("--dry-run", action="store_true",
                    help="show what would upload without calling TikTok")
    tk.set_defaults(func=cmd_tiktok)

    tg = sub.add_parser("telegram",
                        help="send a run's clips to a Telegram chat (for Reels)",
                        parents=[common])
    tg.add_argument("--name", default=None,
                    help="run folder under output/ (auto-detected if only one)")
    tg.add_argument("--format", default="mobile",
                    help="which rendered format to send (default: mobile)")
    tg.add_argument("--channel", default=None,
                    help="niche channel key from [telegram.channels] to send to "
                         "(e.g. 'pojok-informasi'); overrides the project's "
                         "[telegram] channel and the default chat")
    tg.add_argument("--clips", default=None,
                    help="only send these clip numbers, e.g. '1-10' or '1,3,5' "
                         "(default: all clips in the manifest)")
    tg.add_argument("--whoami", action="store_true",
                    help="print the chat_id(s) that have messaged your bot")
    tg.add_argument("--dry-run", action="store_true",
                    help="show what would send without calling Telegram")
    tg.set_defaults(func=cmd_telegram)

    tr = sub.add_parser("track",
                        help="rebuild the campaign/clip status Google Sheet from "
                             "all manifests (auto-runs after upload/tiktok/telegram/run)",
                        parents=[common])
    tr.add_argument("--sync", action="store_true",
                    help="(default action) push to the sheet; without it, still syncs")
    tr.add_argument("--dry-run", action="store_true",
                    help="print the campaign rollup locally without touching Google")
    tr.set_defaults(func=cmd_track)

    cl = sub.add_parser("claim",
                        help="take ownership of a run's clips (producer); sets Owner "
                             "so the reviewer must be someone else (four-eyes)",
                        parents=[common])
    cl.add_argument("--name", default=None, help="run folder under output/")
    cl.add_argument("--clips", default=None, help="only these, e.g. '1-10' or '1,3'")
    cl.add_argument("--by", default=None, help="your name (else env CLIPCRAFT_USER)")
    cl.set_defaults(func=cmd_claim)

    ra = sub.add_parser("rate",
                        help="reviewer verdict: --pass (approved) or --redo (changes), "
                             "with optional --score 1-5; enforces four-eyes",
                        parents=[common])
    ra.add_argument("--name", default=None, help="run folder under output/")
    ra.add_argument("--clips", default=None, help="which clip(s), e.g. '1' or '1,3,5'")
    ra.add_argument("--by", default=None, help="your name (else env CLIPCRAFT_USER)")
    ra.add_argument("--score", type=int, default=None, help="1-5 rating")
    ra.add_argument("--note", default=None, help="short reason / feedback")
    grp = ra.add_mutually_exclusive_group()
    grp.add_argument("--pass", dest="pass_", action="store_true",
                     help="verdict PASS -> status approved (default)")
    grp.add_argument("--redo", action="store_true",
                     help="verdict REDO -> status changes")
    ra.add_argument("--force", action="store_true",
                    help="override the four-eyes guard (rate your own clip)")
    ra.set_defaults(func=cmd_rate)
    return p


def main(argv=None) -> int:
    # Windows consoles default to cp1252, which crashes on emoji / smart quotes the
    # model may put in a title or hook. Force UTF-8 and never let output encoding
    # abort a run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    from .select import OllamaUnreachable
    try:
        return args.func(args)
    except OllamaUnreachable as exc:
        print(f"\n[clipcraft] {exc}")
        return 3
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
