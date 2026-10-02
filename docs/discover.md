# Discover — topic-based source finding

Finds **fresh, long-form** videos on a topic and feeds the good ones into the
clip pipeline. It never bulk-downloads "all of YouTube" — it lists the top N by
views per topic per day and lets you approve which to pull.

```
config.toml [[discover.topics]]  (name, query, project)
        │  py -3 discover.py --scan   (daily)
        ▼
projects/<project>/_DISCOVER_QUEUE.md   ranked list, one [ ] line per video
        │  you tick [x] the ones you want
        │  py -3 discover.py --pull
        ▼
projects/<project>/inbox/   downloaded source  →  auto-runner clips it
```

## One-time setup: YouTube Data API key

1. Go to <https://console.cloud.google.com/> and pick your existing project
   (the same one your upload OAuth client lives in is fine).
2. **APIs & Services → Library →** search **"YouTube Data API v3" → Enable**.
3. **APIs & Services → Credentials → Create credentials → API key**. Copy it
   (looks like `AIza...`). (Optional: restrict it to the YouTube Data API.)
4. Put it in one of: `[discover].api_key` in `config.toml`, env `YOUTUBE_API_KEY`,
   or a `.youtube_api_key` file at the repo root.

Quota: the free tier is 10,000 units/day. Each topic scan costs ~200 units
(two searches + detail lookups), so dozens of topics/day fit easily.

## Daily use

```bash
py -3 discover.py --scan            # all topics; or --topic comedy
#   open projects/<project>/_DISCOVER_QUEUE.md, change - [ ] to - [x] on keepers
py -3 discover.py --pull            # downloads ticked ones into inbox/
#   or --project "Pojok Jenaka" to pull just one project's queue
```

Ticked lines become `- [pulled]` after a successful download, so re-running
`--pull` never double-downloads. A per-project `.discover_seen.json` records
listed video IDs so `--scan` never re-lists the same video on later days.

## Config ([discover])

| Key | Meaning |
|-----|---------|
| `api_key` | YouTube Data API key (or env / file, above) |
| `region` / `language` | search bias (default `ID` / `id`) |
| `published_within_hours` | freshness window (default 24) |
| `min_duration_s` | skip anything shorter (default 600 = 10 min; kills Shorts) |
| `min_views` | optional floor (default 0 — <24h videos have few views) |
| `max_per_topic` | how many top-by-views to queue per topic per run |
| `[[discover.topics]]` | one block per topic: `name`, `query`, `project` |

## Automate the scan (optional)

Add a daily scheduled task like the auto-runner's, pointing at
`py -3 discover.py --scan`, e.g.:

```
schtasks /Create /TN "ClipSystem-Discover" /TR "\"D:\MeSelf\Clip System\_runner\discover.cmd\"" /SC DAILY /ST 08:00 /F
```

Keep **`--pull` manual** — it's the approval gate. Downloading + clipping stays
opt-in so you never pull off-topic or junk videos automatically.

## Notes / limits

- The API's `videoDuration` only buckets short(<4m)/medium(4–20m)/long(>20m), so
  the scan queries **medium + long** and then filters precisely on real duration
  ≥ `min_duration_s`.
- Very fresh videos (<24h) have low view counts, so ranking-by-views is weak on
  the newest uploads; widen `published_within_hours` if you want stronger signal.
- Downloading YouTube videos is against YouTube's ToS; this reuses your existing
  ingest/yt-dlp workflow. Use on content you have the right to repurpose.
