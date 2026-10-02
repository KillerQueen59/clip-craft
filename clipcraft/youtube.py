"""Bulk-upload rendered clips to YouTube as Shorts.

A clip is treated as a Short automatically when it is vertical (9:16) and under
3 minutes — which the `mobile` format already is. This module just pushes each
file through the YouTube Data API v3 (videos.insert).

One-time setup (see README): a Google Cloud project with the YouTube Data API
enabled and an OAuth "Desktop app" client. Drop the downloaded client secret at
the project root as `client_secret.json`. First run opens a browser to
authorize; the token is cached at `.youtube_token.json`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _title_for(clip: dict, cfg: Config) -> str:
    prefix = cfg.raw.get("upload", {}).get("title_prefix", "").strip()
    t = (clip.get("title") or clip.get("hook") or "Clip").strip()
    full = f"{prefix} {t}".strip() if prefix else t
    return (full[:97] + "...") if len(full) > 100 else full


def _description_for(clip: dict, cfg: Config) -> str:
    up = cfg.raw.get("upload", {})
    parts = []
    # Per-clip description: the model's `description`, else its reason.
    body = (clip.get("description") or clip.get("why") or "").strip()
    if body:
        parts.append(body)
    tags = up.get("hashtags", ["#Shorts"])
    if tags:
        parts.append(" ".join(tags))
    if up.get("description_footer"):
        parts.append(up["description_footer"].strip())
    return "\n\n".join(parts)[:4900]


def _account_cfg(cfg: Config, account: str | None) -> dict:
    """Resolve the client_secret filename and token path for an account.

    An account is a named entry under [upload.accounts.<name>]. Each maps to its
    own cached token so multiple channels can be uploaded to without re-login.
    With no accounts configured, behaviour is unchanged: the single
    `.youtube_token.json` and the top-level [upload] client_secret.
    """
    up = cfg.raw.get("upload", {})
    accounts = up.get("accounts", {})
    name = account or up.get("default_account", "main")
    acc = accounts.get(name, {})
    if account and account not in accounts and accounts:
        raise RuntimeError(
            f"Unknown upload account '{account}'. Configured: "
            f"{', '.join(accounts) or '(none)'}"
        )
    token_name = acc.get("token", ".youtube_token.json"
                          if name in ("main", "") else f".youtube_token_{name}.json")
    secret_name = acc.get("client_secret", up.get("client_secret", "client_secret.json"))
    return {"name": name, "token": token_name, "client_secret": secret_name}


def _get_service(cfg: Config, account: str | None = None):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "YouTube upload needs extra packages. Install with:\n"
            "  py -3 -m pip install google-api-python-client google-auth-oauthlib"
        ) from exc

    acc = _account_cfg(cfg, account)
    print(f"[upload] account: {acc['name']} (token {acc['token']})")
    secret = cfg.root / acc["client_secret"]
    if not secret.exists():
        # Google downloads the file with a long name; auto-discover it.
        matches = sorted(cfg.root.glob("client_secret*.json"))
        if matches:
            secret = matches[0]
            print(f"[upload] using OAuth client: {secret.name}")
    token = cfg.root / acc["token"]
    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not secret.exists():
                raise RuntimeError(
                    f"Missing OAuth client secret at {secret}. Create an OAuth "
                    "'Desktop app' client in Google Cloud and save it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
            creds = flow.run_local_server(port=0)
        token.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def upload_batch(jobs, cfg: Config, manifest_path: Path, clips: list[dict],
                 privacy: str = "unlisted", spacing: int = 60,
                 dry_run: bool = False, account: str | None = None) -> int:
    print(f"[upload] {len(jobs)} clip(s) -> YouTube ({privacy})"
          + (" [dry-run]" if dry_run else ""))
    for clip, _ in jobs:
        print(f"  clip {clip.get('clip')}: {_title_for(clip, cfg)}")
    if dry_run:
        print("[upload] dry-run only; nothing was uploaded.")
        return 0

    from googleapiclient.http import MediaFileUpload

    acc_name = _account_cfg(cfg, account)["name"]
    service = _get_service(cfg, account)
    uploaded = 0
    for i, (clip, path) in enumerate(jobs):
        body = {
            "snippet": {
                "title": _title_for(clip, cfg),
                "description": _description_for(clip, cfg),
                "categoryId": str(cfg.raw.get("upload", {}).get("category_id", 22)),
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(path, chunksize=-1, resumable=True)
        try:
            req = service.videos().insert(part="snippet,status", body=body, media_body=media)
            resp = req.execute()
            vid = resp["id"]
            url = f"https://youtu.be/{vid}"
            rec = {"id": vid, "url": url, "privacy": privacy}
            # Track per-account so the same clip can go to multiple channels and the
            # skip-guard only skips the account it was already uploaded to.
            ups = clip.get("uploads") or {}
            ups[acc_name] = rec
            clip["uploads"] = ups
            clip["youtube"] = rec  # latest upload, kept for backward compatibility
            uploaded += 1
            print(f"[upload] clip {clip.get('clip')} -> {url}")
            # Set a custom thumbnail (16:9 sharp-frame card). Needs a
            # verification-enabled channel; if it isn't, the API rejects the call
            # — the video still uploaded fine, so just warn and move on.
            thumb = clip.get("thumbnail") or (clip.get("thumbnails") or {}).get("mobile")
            if thumb and Path(thumb).exists():
                try:
                    service.thumbnails().set(
                        videoId=vid,
                        media_body=MediaFileUpload(thumb)).execute()
                    print(f"[upload] clip {clip.get('clip')} thumbnail set")
                except Exception as exc:
                    print(f"[upload] clip {clip.get('clip')} thumbnail SKIPPED "
                          f"(channel may not allow custom thumbnails): {exc}")
        except Exception as exc:
            print(f"[upload] clip {clip.get('clip')} FAILED: {exc}")
        manifest_path.write_text(json.dumps(clips, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        if spacing and i < len(jobs) - 1:
            print(f"[upload] waiting {spacing}s before next upload...")
            time.sleep(spacing)

    print(f"[upload] done: {uploaded}/{len(jobs)} uploaded. URLs saved to manifest.json")
    from . import tracker
    tracker.sync_safe(cfg)
    return 0 if uploaded else 1
