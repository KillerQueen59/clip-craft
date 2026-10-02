"""Campaign / clip status tracker that syncs to a Google Sheet.

A single source of truth for "what campaigns are we running, are they done, what
clips exist, where were they posted". The data already lives in every project's
clips/manifest.json plus the [DONE] file-rename marker, so this module only reads
and aggregates it — nothing new to record by hand.

Two tabs are written:
  * Campaigns — one row per project: clip count, per-platform posted count, #DONE.
  * Clips     — one row per clip: title, duration, YouTube link(s), TikTok /
                Telegram status, DONE.

Auth is a Google service account (no token expiry). Config lives under [tracking]
in the base config.toml:

    [tracking]
    enabled = true
    sheet_id = "<the id from the sheet URL>"
    service_account = ".gsheets_service_account.json"   # relative to the repo root

`sync(cfg)` rebuilds both tabs from all manifests. `sync_safe(cfg)` is the same but
never raises — the pipeline calls it after upload/tiktok/telegram/run so a Sheets
hiccup can never break an upload.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

CLIP_HEADER = ["Campaign", "Run", "Clip", "Title", "Duration (s)",
               "YouTube", "TikTok", "Telegram", "DONE",
               "Owner", "Status", "Reviewer", "Score", "Note", "Source raw"]
CAMP_HEADER = ["Campaign", "Runs", "Clips", "YouTube posted", "TikTok posted",
               "Telegram posted", "DONE", "Synced (UTC)"]

# Columns humans (or the review/claim CLI) own. The sync MERGES these: it never
# wipes a value already in the sheet — it only fills one in from the manifest when
# the sheet cell is blank. Machine columns (Title, YouTube, ... DONE) are always
# refreshed from the manifest. Keyed on (Campaign, Run, Clip).
HUMAN_COLS = ["Owner", "Status", "Reviewer", "Score", "Note"]


def _done_marker(clips_dir: Path, n: int) -> bool:
    """A clip is 'done/posted' when its mobile file was renamed '[DONE] ...' — the
    library-wide posted marker. The rename de-syncs the manifest path, so detect it
    from the actual files, not the manifest."""
    try:
        for p in clips_dir.glob(f"*clip_{n:02d}_mobile.mp4"):
            if p.name.startswith("[DONE]"):
                return True
    except OSError:
        pass
    return False


def _yt_links(clip: dict) -> str:
    ups = clip.get("uploads") or {}
    parts = []
    for acct, rec in ups.items():
        url = (rec or {}).get("url")
        if url:
            parts.append(f"{acct}: {url}")
    if not parts:
        yt = clip.get("youtube") or {}
        if yt.get("url"):
            parts.append(yt["url"])
    return "\n".join(parts)


def collect(root: Path) -> tuple[list[list], list[list]]:
    """Walk projects/ and return (clip_rows, campaign_rows) including headers."""
    projects = root / "projects"
    clip_rows: list[list] = [CLIP_HEADER]
    camp: dict[str, dict] = {}

    manifests = sorted(p for p in projects.rglob("manifest.json")
                       if p.parent.name == "clips")
    for mf in manifests:
        rel = mf.relative_to(projects).parts
        if "output" not in rel:
            continue
        oi = rel.index("output")
        campaign = "/".join(rel[:oi]) or "(root)"
        run = rel[oi + 1] if len(rel) > oi + 1 else ""
        clips_dir = mf.parent
        try:
            clips = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(clips, list):
            continue

        c = camp.setdefault(campaign, {"runs": set(), "clips": 0, "yt": 0,
                                       "tt": 0, "tg": 0, "done": 0})
        c["runs"].add(run)
        for clip in clips:
            n = clip.get("clip")
            if n is None:
                continue
            yt = _yt_links(clip)
            tt = (clip.get("tiktok") or {}).get("status", "")
            tg = (clip.get("telegram") or {}).get("status", "")
            done = _done_marker(clips_dir, n)
            dur = clip.get("duration")
            if dur is None and clip.get("start") is not None and clip.get("end") is not None:
                dur = round(float(clip["end"]) - float(clip["start"]), 1)
            raw = clip.get("raw") or ""
            rv = clip.get("review") or {}
            score = rv.get("score", "")
            clip_rows.append([
                campaign, run, n, clip.get("title", ""),
                dur if dur is not None else "", yt, tt, tg,
                "YES" if done else "",
                clip.get("owner", ""), clip.get("status", ""),
                rv.get("by", ""), "" if score in (None, "") else score,
                rv.get("note", ""),
                Path(raw).name if raw else "",
            ])
            c["clips"] += 1
            if yt:
                c["yt"] += 1
            if tt:
                c["tt"] += 1
            if tg:
                c["tg"] += 1
            if done:
                c["done"] += 1

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    camp_rows: list[list] = [CAMP_HEADER]
    for name in sorted(camp):
        s = camp[name]
        camp_rows.append([name, len(s["runs"]), s["clips"], s["yt"],
                          s["tt"], s["tg"], s["done"], now])
    return clip_rows, camp_rows


def _service(cfg: Config):
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    tcfg = cfg.raw.get("tracking", {})
    sid = tcfg.get("sheet_id", "").strip()
    if not sid:
        raise RuntimeError("[tracking] sheet_id is not set in config.toml.")
    sa = cfg.root / tcfg.get("service_account", ".gsheets_service_account.json")
    if not sa.exists():
        raise RuntimeError(f"service account key not found: {sa}")
    creds = Credentials.from_service_account_file(str(sa), scopes=SCOPES)
    return build("sheets", "v4", credentials=creds), sid


def _ensure_tabs(svc, sid: str, tabs: list[str]) -> None:
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    have = {s["properties"]["title"] for s in meta.get("sheets", [])}
    reqs = [{"addSheet": {"properties": {"title": t}}} for t in tabs if t not in have]
    if reqs:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sid, body={"requests": reqs}).execute()


def _write_tab(svc, sid: str, tab: str, rows: list[list]) -> None:
    svc.spreadsheets().values().clear(
        spreadsheetId=sid, range=f"'{tab}'").execute()
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range=f"'{tab}'!A1",
        valueInputOption="RAW", body={"values": rows}).execute()


def _read_human(svc, sid: str) -> dict:
    """Read the existing Clips tab and return {(campaign, run, clip): {col: val}}
    for the HUMAN_COLS only, matched by column NAME (so it survives header changes
    and the pre-upgrade layout). Used to merge — never wipe — human/review edits."""
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sid, range="'Clips'").execute()
    except Exception:  # noqa: BLE001 — sheet/tab may not exist yet
        return {}
    rows = resp.get("values", [])
    if not rows:
        return {}
    header = rows[0]

    def col(name: str) -> int:
        return header.index(name) if name in header else -1

    ic, ir, icl = col("Campaign"), col("Run"), col("Clip")
    if ic < 0 or icl < 0:
        return {}
    hidx = {h: col(h) for h in HUMAN_COLS}
    out: dict = {}
    for row in rows[1:]:
        def cell(j):
            return row[j] if 0 <= j < len(row) else ""
        key = (cell(ic), cell(ir), str(cell(icl)))
        vals = {h: cell(hidx[h]) for h in HUMAN_COLS if hidx[h] >= 0}
        if any(str(v).strip() for v in vals.values()):
            out[key] = vals
    return out


def _merge_human(clip_rows: list[list], existing: dict) -> None:
    """Backfill each new row's HUMAN_COLS from the existing sheet when the
    manifest left them blank, so a manual sheet edit is never clobbered."""
    if not existing:
        return
    header = clip_rows[0]
    ic, ir, icl = (header.index("Campaign"), header.index("Run"),
                   header.index("Clip"))
    hidx = {h: header.index(h) for h in HUMAN_COLS}
    for row in clip_rows[1:]:
        key = (row[ic], row[ir], str(row[icl]))
        old = existing.get(key)
        if not old:
            continue
        for h in HUMAN_COLS:
            j = hidx[h]
            if j < len(row) and not str(row[j]).strip():
                row[j] = old.get(h, "")


def sync(cfg: Config) -> int:
    """Rebuild both tabs from all manifests, MERGING human/review columns from the
    current sheet so manual edits and CLI-set review state are never wiped."""
    clip_rows, camp_rows = collect(cfg.root)
    svc, sid = _service(cfg)
    _ensure_tabs(svc, sid, ["Campaigns", "Clips"])
    _merge_human(clip_rows, _read_human(svc, sid))
    _write_tab(svc, sid, "Campaigns", camp_rows)
    _write_tab(svc, sid, "Clips", clip_rows)
    return len(clip_rows) - 1


def sync_safe(cfg: Config) -> None:
    """Best-effort sync for the upload/render code paths: never raises, only warns
    when tracking is enabled and something goes wrong."""
    tcfg = cfg.raw.get("tracking", {})
    if not tcfg.get("enabled", False):
        return
    try:
        n = sync(cfg)
        print(f"[track] synced {n} clip(s) to Google Sheet")
    except Exception as exc:  # noqa: BLE001 — tracking must never break an upload
        print(f"[track] sheet sync skipped: {exc}")
