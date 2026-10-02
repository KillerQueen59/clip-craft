"""Push rendered clips to a creator's TikTok inbox as drafts.

Uses the Content Posting API "inbox" method (video.upload scope): each clip lands
in the creator's TikTok inbox and they finish posting in-app. This works in the
app's sandbox for added Target Users, so it does NOT need the public-post audit
that Direct Post (video.publish) requires.

TikTok requires an HTTPS redirect URI (no localhost/http), so auth is a two-step
manual code paste instead of a local callback server:

  1. `clipcraft tiktok --auth`        -> prints an authorize URL. Open it, approve.
     TikTok redirects to your registered HTTPS redirect_uri with `?code=...` in the
     address bar (the page itself can fail to load — only the URL matters).
  2. `clipcraft tiktok --code "<paste the code OR the full redirected URL>"`
     -> exchanges it for a token, cached at `.tiktok_token.json`.
  3. `clipcraft tiktok`               -> uploads the run's clips to the inbox.

Setup (developers.tiktok.com): an app with Login Kit + Content Posting API, scopes
`user.info.basic` + `video.upload`, and an HTTPS redirect URI matching [tiktok]
redirect_uri. Put client_key/client_secret in [tiktok] (or env TIKTOK_CLIENT_KEY /
TIKTOK_CLIENT_SECRET).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Config

_AUTH_TMP = ".tiktok_auth.json"  # holds the PKCE verifier/state between the two steps

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
SCOPES = "user.info.basic,video.upload"


def _creds(cfg: Config) -> tuple[str, str, str]:
    tk = cfg.raw.get("tiktok", {})
    key = os.environ.get("TIKTOK_CLIENT_KEY") or tk.get("client_key", "")
    secret = os.environ.get("TIKTOK_CLIENT_SECRET") or tk.get("client_secret", "")
    redirect = tk.get("redirect_uri", "http://localhost:8080/callback")
    if not key or not secret:
        raise RuntimeError(
            "Missing TikTok client_key/client_secret. Put them in [tiktok] in "
            "config.toml, or set env TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET.")
    return key, secret, redirect


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def build_auth_url(cfg: Config) -> str:
    """Step 1: create the authorize URL and stash the PKCE verifier/state so the
    later --code step can complete the exchange."""
    key, _secret, redirect = _creds(cfg)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    (cfg.root / _AUTH_TMP).write_text(
        json.dumps({"verifier": verifier, "state": state, "redirect": redirect}),
        encoding="utf-8")
    params = {
        "client_key": key, "scope": SCOPES, "response_type": "code",
        "redirect_uri": redirect, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _extract_code(code_or_url: str, expected_state: str | None) -> str:
    s = code_or_url.strip()
    if s.startswith("http"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(s).query)
        code = (q.get("code") or [None])[0]
        state = (q.get("state") or [None])[0]
        if expected_state and state and state != expected_state:
            raise RuntimeError("state mismatch — re-run `--auth` and use its fresh URL.")
        if not code:
            raise RuntimeError("no `?code=` found in that URL. Paste the full "
                               "redirected URL, or just the code value.")
        return code
    return s.split("&")[0].split("=")[-1] if "=" in s else s


def complete_auth(cfg: Config, code_or_url: str) -> dict:
    """Step 2: exchange the pasted code for a token and cache it."""
    key, secret, redirect = _creds(cfg)
    tmp = cfg.root / _AUTH_TMP
    saved = json.loads(tmp.read_text(encoding="utf-8")) if tmp.exists() else {}
    code = _extract_code(code_or_url, saved.get("state"))
    resp = _post_form(TOKEN_URL, {
        "client_key": key, "client_secret": secret, "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": saved.get("redirect", redirect),
        "code_verifier": saved.get("verifier", ""),
    })
    tok = _store_token(cfg.root / ".tiktok_token.json", resp)
    if tmp.exists():
        tmp.unlink()
    return tok


def _store_token(tokfile: Path, resp: dict) -> dict:
    if "access_token" not in resp:
        raise RuntimeError(f"TikTok token error: {resp}")
    resp["expires_at"] = time.time() + int(resp.get("expires_in", 86400))
    tokfile.write_text(json.dumps(resp, indent=2), encoding="utf-8")
    return resp


def _get_token(cfg: Config) -> str:
    key, secret, _redirect = _creds(cfg)
    tokfile = cfg.root / ".tiktok_token.json"
    tok = json.loads(tokfile.read_text(encoding="utf-8")) if tokfile.exists() else None
    if tok and tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60:
        return tok["access_token"]
    if tok and tok.get("refresh_token"):
        try:
            resp = _post_form(TOKEN_URL, {
                "client_key": key, "client_secret": secret,
                "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
            })
            return _store_token(tokfile, resp)["access_token"]
        except Exception as exc:
            print(f"[tiktok] token refresh failed ({exc}); re-authorize below.")
    raise RuntimeError(
        "No TikTok token yet. Authorize first (two steps):\n"
        "  1) py -3 -m clipcraft tiktok --project <name> --auth\n"
        "  2) py -3 -m clipcraft tiktok --project <name> --code \"<code-or-redirected-URL>\"")


_MAX_CHUNK = 64 * 1024 * 1024   # TikTok caps a single chunk at 64 MB


def _upload_one(access_token: str, path: Path) -> str:
    """Init an inbox upload and PUT the file. Returns the publish_id. Files up to
    64 MB go as one chunk; larger files are split into equal chunks (the final
    chunk absorbs the remainder), as TikTok rejects any single chunk over 64 MB."""
    size = path.stat().st_size
    if size <= _MAX_CHUNK:
        chunk_size, total_chunks = size, 1
    else:
        # chunk_size must stay in [5MB, 64MB]; total = size // chunk_size and the
        # last chunk carries whatever is left over (so it may exceed chunk_size).
        total_chunks = -(-size // _MAX_CHUNK)          # ceil
        chunk_size = size // total_chunks
    init_body = json.dumps({
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        }
    }).encode()
    req = urllib.request.Request(INBOX_INIT_URL, data=init_body, headers={
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        init = json.loads(r.read().decode("utf-8"))
    data = init.get("data") or {}
    publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
    if not upload_url:
        raise RuntimeError(f"inbox init failed: {init}")

    blob = path.read_bytes()
    for i in range(total_chunks):
        lo = i * chunk_size
        hi = size - 1 if i == total_chunks - 1 else lo + chunk_size - 1
        part = blob[lo:hi + 1]
        put = urllib.request.Request(upload_url, data=part, method="PUT", headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(len(part)),
            "Content-Range": f"bytes {lo}-{hi}/{size}",
        })
        with urllib.request.urlopen(put, timeout=300) as r:
            r.read()
    return publish_id


def _explain(exc: Exception) -> str:
    """Surface TikTok's real error message from an HTTPError body, not just the code."""
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = json.loads(exc.read().decode())
            err = detail.get("error", {})
            msg = err.get("message") or err.get("code")
            if msg:
                return f"{msg} (HTTP {exc.code})"
        except Exception:
            pass
    return str(exc)


def _upload_with_retry(access_token: str, path: Path, retries: int = 4) -> str:
    """Upload one clip, backing off on HTTP 429 (sandbox rate limit)."""
    import urllib.error

    for attempt in range(retries + 1):
        try:
            return _upload_one(access_token, path)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                wait = 30 * (attempt + 1)
                print(f"[tiktok] rate limited (429); waiting {wait}s then retrying...")
                time.sleep(wait)
                continue
            raise


def upload_batch(jobs, cfg: Config, manifest_path: Path, clips: list[dict],
                 dry_run: bool = False) -> int:
    print(f"[tiktok] {len(jobs)} clip(s) -> TikTok inbox (draft)"
          + (" [dry-run]" if dry_run else ""))
    for clip, _ in jobs:
        print(f"  clip {clip.get('clip')}: {(clip.get('title') or '').strip()}")
    if dry_run:
        print("[tiktok] dry-run only; nothing was uploaded.")
        return 0

    token = _get_token(cfg)
    uploaded = 0
    for i, (clip, path) in enumerate(jobs):
        try:
            pid = _upload_with_retry(token, Path(path))
            clip["tiktok"] = {"publish_id": pid, "status": "inbox"}
            uploaded += 1
            print(f"[tiktok] clip {clip.get('clip')} -> inbox draft (publish_id={pid})")
        except Exception as exc:
            print(f"[tiktok] clip {clip.get('clip')} FAILED: {_explain(exc)}")
        manifest_path.write_text(json.dumps(clips, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        if i < len(jobs) - 1:
            time.sleep(10)  # sandbox rate limit is ~6/min; stay well under it

    print(f"[tiktok] done: {uploaded}/{len(jobs)} pushed to inbox. Open the TikTok "
          "app on the target account -> notifications to finish posting each draft.")
    from . import tracker
    tracker.sync_safe(cfg)
    return 0 if uploaded else 1
