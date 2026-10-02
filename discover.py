#!/usr/bin/env python3
"""Topic-based source discovery for the Clip System (YouTube Data API v3).

Two modes:

    py -3 discover.py --scan          # search every configured topic, append
                                      # fresh candidates to each project's
                                      # _DISCOVER_QUEUE.md (ranked by views)
    py -3 discover.py --pull          # download every [x]-ticked queue line
                                      # into that project's inbox/ (auto-runner
                                      # then clips it) and mark it pulled

Config lives in config.toml under [discover] (+ [[discover.topics]]). API key
is read from [discover].api_key, env YOUTUBE_API_KEY, or a .youtube_api_key
file. See docs/discover.md.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECTS_DIR = ROOT / "projects"
CONFIG = ROOT / "config.toml"
API = "https://www.googleapis.com/youtube/v3"

QUEUE_NAME = "_DISCOVER_QUEUE.md"
SEEN_NAME = ".discover_seen.json"  # per project: video ids already listed


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _load_cfg() -> dict:
    with CONFIG.open("rb") as fh:
        return tomllib.load(fh).get("discover", {})


def _api_key(cfg: dict) -> str:
    import os
    key = (cfg.get("api_key") or "").strip()
    if key:
        return key
    key = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
    if key:
        return key
    kf = ROOT / ".youtube_api_key"
    if kf.exists():
        return kf.read_text(encoding="utf-8").strip()
    raise SystemExit(
        "No YouTube Data API key. Set [discover].api_key in config.toml, env "
        "YOUTUBE_API_KEY, or a .youtube_api_key file. See docs/discover.md."
    )


def _get(path: str, params: dict) -> dict:
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


_DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _iso_dur_to_s(iso: str) -> int:
    m = _DUR_RE.fullmatch(iso or "")
    if not m:
        return 0
    h, mn, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mn * 60 + s


def _fmt_dur(s: int) -> str:
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _search_topic(topic: dict, cfg: dict, key: str) -> list[dict]:
    hours = int(cfg.get("published_within_hours", 24))
    after = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours))
    published_after = after.strftime("%Y-%m-%dT%H:%M:%SZ")

    search = _get("search", {
        "key": key, "part": "snippet", "type": "video",
        "q": topic["query"],
        "publishedAfter": published_after,
        "regionCode": cfg.get("region", "ID"),
        "relevanceLanguage": cfg.get("language", "id"),
        "videoDuration": "long",     # >20min at the API; medium+ handled below
        "order": "viewCount",
        "maxResults": 50,
    })
    ids = [it["id"]["videoId"] for it in search.get("items", [])
           if it.get("id", {}).get("videoId")]
    # Also grab "medium" (4-20min) so 10-20min talks aren't lost.
    search2 = _get("search", {
        "key": key, "part": "snippet", "type": "video",
        "q": topic["query"], "publishedAfter": published_after,
        "regionCode": cfg.get("region", "ID"),
        "relevanceLanguage": cfg.get("language", "id"),
        "videoDuration": "medium", "order": "viewCount", "maxResults": 50,
    })
    ids += [it["id"]["videoId"] for it in search2.get("items", [])
            if it.get("id", {}).get("videoId")]
    ids = list(dict.fromkeys(ids))  # de-dup, keep order
    if not ids:
        return []

    min_dur = int(cfg.get("min_duration_s", 600))
    min_views = int(cfg.get("min_views", 0))
    out = []
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        det = _get("videos", {
            "key": key, "part": "contentDetails,statistics,snippet",
            "id": ",".join(batch),
        })
        for v in det.get("items", []):
            dur = _iso_dur_to_s(v.get("contentDetails", {}).get("duration"))
            views = int(v.get("statistics", {}).get("viewCount", 0) or 0)
            if dur < min_dur or views < min_views:
                continue
            sn = v.get("snippet", {})
            out.append({
                "id": v["id"],
                "title": sn.get("title", "").replace("|", "/").strip(),
                "channel": sn.get("channelTitle", "").replace("|", "/").strip(),
                "views": views,
                "dur_s": dur,
                "url": f"https://www.youtube.com/watch?v={v['id']}",
            })
    out.sort(key=lambda r: r["views"], reverse=True)
    return out[:int(cfg.get("max_per_topic", 15))]


def _seen(project: Path) -> set[str]:
    f = project / SEEN_NAME
    if f.exists():
        try:
            return set(json.loads(f.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            return set()
    return set()


def _mark_seen(project: Path, ids: list[str]) -> None:
    f = project / SEEN_NAME
    cur = _seen(project)
    cur.update(ids)
    f.write_text(json.dumps(sorted(cur)), encoding="utf-8")


def _append_queue(project: Path, topic_name: str, rows: list[dict]) -> int:
    project.mkdir(parents=True, exist_ok=True)
    q = project / QUEUE_NAME
    if not q.exists():
        q.write_text(
            "# Discover queue — tick [x] to pull into inbox, then "
            "`py -3 discover.py --pull`\n"
            "# columns: title | channel | views | duration | URL\n\n",
            encoding="utf-8",
        )
    with q.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {topic_name} — {_now()}\n")
        for r in rows:
            fh.write(
                f"- [ ] {r['title']} | {r['channel']} | "
                f"{r['views']:,} views | {_fmt_dur(r['dur_s'])} | {r['url']}\n"
            )
    return len(rows)


def cmd_scan(only_topic: str | None) -> int:
    cfg = _load_cfg()
    key = _api_key(cfg)
    topics = cfg.get("topics", [])
    total = 0
    for topic in topics:
        if only_topic and topic.get("name") != only_topic:
            continue
        project = PROJECTS_DIR / topic["project"]
        rows = _search_topic(topic, cfg, key)
        seen = _seen(project)
        fresh = [r for r in rows if r["id"] not in seen]
        if not fresh:
            print(f"[discover] {topic['name']}: 0 new "
                  f"({len(rows)} found, all already listed)")
            continue
        n = _append_queue(project, topic["name"], fresh)
        _mark_seen(project, [r["id"] for r in fresh])
        total += n
        print(f"[discover] {topic['name']} -> {project.name}: {n} new "
              f"candidate(s) queued")
    print(f"[discover] scan done: {total} new candidate(s) across "
          f"{len(topics)} topic(s)")
    return 0


_URL_RE = re.compile(r"https://www\.youtube\.com/watch\?v=[\w-]+")


def cmd_pull(only_project: str | None) -> int:
    projects = ([PROJECTS_DIR / only_project] if only_project
                else [d for d in sorted(PROJECTS_DIR.iterdir())
                      if d.is_dir() and (d / QUEUE_NAME).exists()])
    pulled = 0
    for project in projects:
        q = project / QUEUE_NAME
        if not q.exists():
            continue
        lines = q.read_text(encoding="utf-8").splitlines()
        changed = False
        inbox = project / "inbox"
        for idx, line in enumerate(lines):
            if not line.strip().startswith("- [x]"):
                continue
            m = _URL_RE.search(line)
            if not m:
                continue
            url = m.group(0)
            inbox.mkdir(parents=True, exist_ok=True)
            print(f"[discover] pull {url} -> {project.name}/inbox/")
            out_tmpl = str(inbox / "%(title).80s [%(id)s].%(ext)s")
            rc = subprocess.run([
                sys.executable, "-m", "yt_dlp",
                "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
                "--merge-output-format", "mp4", "--no-playlist",
                "-o", out_tmpl, url,
            ], cwd=str(ROOT)).returncode
            if rc == 0:
                lines[idx] = line.replace("- [x]", "- [pulled]", 1)
                changed = True
                pulled += 1
            else:
                print(f"[discover] yt-dlp failed rc={rc} for {url}")
        if changed:
            q.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[discover] pull done: {pulled} video(s) into inbox(es)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Topic source discovery.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan", action="store_true",
                      help="search topics, append fresh candidates to queues")
    mode.add_argument("--pull", action="store_true",
                      help="download [x]-ticked queue lines into project inboxes")
    ap.add_argument("--topic", default=None, help="limit --scan to one topic name")
    ap.add_argument("--project", default=None, help="limit --pull to one project")
    args = ap.parse_args()
    return cmd_scan(args.topic) if args.scan else cmd_pull(args.project)


if __name__ == "__main__":
    raise SystemExit(main())
