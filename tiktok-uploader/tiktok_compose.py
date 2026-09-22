#!/usr/bin/env python3
"""tiktok_compose.py — Interactive, review-compliant TikTok publish flow.

TikTok-only companion to gen_rflxn.py. Where the generator bot publishes
automatically, this script walks through the full Direct Post experience
that the TikTok Content Sharing Guidelines expect to see on screen:

  1. OAuth (Login Kit) — user.info.basic + video.publish
  2. Query Creator Info — show only the account's allowed privacy options
  3. Compose — privacy (no default), interaction toggles (off by default),
     editable RFLXN caption, commercial disclosure
  4. Consent — explicit "Music Usage Confirmation" agreement
  5. Preview + publish (FILE_UPLOAD) + status polling

It is intentionally interactive so it can be screen-recorded for the
TikTok App Review demo video. Run inside the project venv:

    python3 tiktok_compose.py --tiktok-auth
    python3 tiktok_compose.py --video /path/to/test_clip.mp4

Keep the demo short: use a small (<50 MB) mp4 test clip so the upload +
status polling complete quickly and the screen recording stays under
TikTok's 50 MB / mp4 limit for uploaded demo material.
"""

import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

# ──────────────────────────────────────────────
# VENV AUTO-BOOTSTRAP
# ──────────────────────────────────────────────

REQUIRED_PACKAGES = ["click", "questionary", "requests"]

VENV_DIR = Path.cwd() / ".venv"


def _in_venv() -> bool:
    return (
        hasattr(sys, "real_prefix")
        or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
    )


def _bootstrap_venv() -> None:
    print("[setup] No active virtual environment detected.")
    if not VENV_DIR.exists():
        print(f"[setup] Creating virtual environment at {VENV_DIR} ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    else:
        print(f"[setup] Using existing virtual environment at {VENV_DIR}")

    pip = VENV_DIR / "bin" / "pip"
    print("[setup] Upgrading pip ...")
    subprocess.check_call([str(pip), "install", "--upgrade", "pip"])
    for pkg in REQUIRED_PACKAGES:
        subprocess.check_call([str(pip), "install", pkg])

    python = VENV_DIR / "bin" / "python3"
    if not python.exists():
        python = VENV_DIR / "bin" / "python"
    print(f"[setup] Re-executing inside venv: {python}")
    os.execv(str(python), [str(python), __file__, *sys.argv[1:]])


if not _in_venv():
    _bootstrap_venv()

# ---- imports that require installed packages ----
from urllib.parse import quote  # noqa: E402

import click  # noqa: E402
import questionary  # noqa: E402
import requests  # noqa: E402

# ──────────────────────────────────────────────
# BRAND + TIKTOK CONFIG
# ──────────────────────────────────────────────

ARTIST_NAME = "RFLXN"
LOCATION = "Palmdale, CA"
GENRES = "triphop & progressive melodic lofi"

CREDENTIALS_FILE = Path.home() / ".itsrflxn_tiktok.json"

TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
# Web redirect registered in the TikTok portal (required for review): the
# GitHub Pages callback page forwards the code to the local loopback listener
# on port 8699. Both must be registered in the portal for the desktop flow.
TIKTOK_REDIRECT_URI = os.environ.get(
    "TIKTOK_REDIRECT_URI",
    "https://rtmalikian.github.io/itsrflxn-tiktok-api/callback/",
)
TIKTOK_SCOPES = "user.info.basic,video.publish"

TIKTOK_OAUTH_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_VIDEO_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
TIKTOK_CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"

TIKTOK_SITE_BASE = "https://rtmalikian.github.io/itsrflxn-tiktok-api"

TIKTOK_UPLOAD_CHUNK_SIZE = 64 * 1024 * 1024  # 64 MiB chunk ceiling
TIKTOK_STATUS_POLL_SEC = 5
TIKTOK_STATUS_TIMEOUT = 10 * 60

MUSIC_CONSENT_TEXT = "By posting, you agree to TikTok's Music Usage Confirmation"

TIKTOK_HASHTAGS = [
    f"#{ARTIST_NAME}",
    "#RFLXNsounds",
    "#Triphop",
    "#TripHop",
    "#Lofi",
    "#MelodicLofi",
    "#ProgressiveLofi",
    "#LofiBeats",
    "#NewMusic",
    "#MusicOnTikTok",
    "#StreamingNow",
    "#Palmdale",
    "#PalmdaleCA",
    "#California",
    "#MusicDrop",
    "#ViralMusic",
    "#FYP",
    "#ForYou",
]


def build_caption(opener: str) -> str:
    """Branded RFLXN caption: music CTA + Palmdale location + hashtags."""
    return "\n".join([
        opener.strip() or "RFLXN — new sounds dropping",
        "",
        f"🚨 {ARTIST_NAME} is back with new sounds 🚨",
        f"{GENRES.capitalize()} — the {ARTIST_NAME} sound",
        f"Listen to {ARTIST_NAME} on ALL streaming platforms "
        "(Spotify, Apple Music, YouTube Music, and more)",
        f"You can also pick any {ARTIST_NAME} song as the sound for YOUR TikTok!",
        "",
        f"📍 {LOCATION}",
        "",
        " ".join(TIKTOK_HASHTAGS),
    ])


# ──────────────────────────────────────────────
# OAUTH (LOGIN KIT)
# ──────────────────────────────────────────────


def _request_oauth(payload: dict) -> dict:
    resp = requests.post(TIKTOK_OAUTH_TOKEN_URL, data=payload, timeout=30)
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"TikTok OAuth failed: {data}")
    return data


def _capture_local_callback_code(port: int, expected_state: str) -> str | None:
    """Briefly run a local HTTP server to catch the OAuth redirect."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            captured["code"] = query.get("code", [None])[0]
            captured["state"] = query.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Authorisation complete</h1>"
                b"<p>You can close this tab.</p></body></html>"
            )

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 300
    try:
        while captured.get("code") is None:
            server.handle_request()
    except OSError:
        return None
    finally:
        server.server_close()

    if captured.get("state") != expected_state:
        print("[error] OAuth state mismatch — aborting.")
        return None
    return captured.get("code")


def authorize_tiktok() -> dict:
    """Run the TikTok authorisation-code flow and persist the tokens.

    The loopback listener always runs: when the redirect URI is the
    registered web URL (e.g. the GitHub Pages callback page), that page
    forwards the code here; when the redirect URI is localhost directly,
    the browser lands here first. If no callback arrives, fall back to
    pasting the code from the redirected URL.
    """
    client_key = TIKTOK_CLIENT_KEY or click.prompt("TikTok client key")
    client_secret = TIKTOK_CLIENT_SECRET or click.prompt("TikTok client secret", hide_input=True)
    redirect_uri = TIKTOK_REDIRECT_URI
    state = secrets.token_urlsafe(16)

    auth_url = (
        f"{TIKTOK_AUTH_URL}?client_key={quote(client_key)}"
        f"&scope={quote(TIKTOK_SCOPES)}"
        f"&response_type=code&redirect_uri={quote(redirect_uri)}&state={state}"
    )
    print(f"\nOpen this URL in your browser and authorise the app:\n  {auth_url}\n")

    m = re.search(r":(\d+)", redirect_uri)
    port = int(m.group(1)) if m else 8699

    code = None
    try:
        code = _capture_local_callback_code(port, state)
    except OSError as e:
        print(f"[error] Could not start the local callback server: {e}")
    if code is None:
        code = click.prompt("Paste the `code` parameter from the redirected URL")

    if not code:
        raise RuntimeError("Authorisation code was never obtained.")

    data = _request_oauth({
        "client_key": client_key,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    })

    creds = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "open_id": data.get("open_id", ""),
        "client_key": client_key,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "scopes": data.get("scope", ""),
        "expires_at": time.time() + int(data.get("expires_in", 86400)),
    }
    _save_creds(creds)
    print("[tiktok] OAuth complete — credentials saved.")
    return creds


def _load_creds() -> dict | None:
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        return json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_creds(creds: dict) -> None:
    CREDENTIALS_FILE.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    CREDENTIALS_FILE.chmod(0o600)


def get_access_token() -> dict:
    """Return fresh credentials, refreshing the access token when close to expiry."""
    creds = _load_creds()
    if creds is None:
        return authorize_tiktok()
    if creds.get("expires_at", 0) <= time.time() + 60:
        if not creds.get("refresh_token"):
            raise RuntimeError("Access token expired and no refresh token exists — "
                               "re-run with --tiktok-auth.")
        data = _request_oauth({
            "client_key": creds["client_key"],
            "client_secret": creds["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": creds["refresh_token"],
        })
        creds["access_token"] = data["access_token"]
        if data.get("refresh_token"):
            creds["refresh_token"] = data["refresh_token"]
        creds["expires_at"] = time.time() + int(data.get("expires_in", 86400))
        _save_creds(creds)
        print("[tiktok] Access token refreshed.")
    return creds


# ──────────────────────────────────────────────
# CONTENT POSTING API HELPERS
# ──────────────────────────────────────────────


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def query_creator_info(access_token: str) -> dict:
    resp = requests.post(
        TIKTOK_CREATOR_INFO_URL, headers=_headers(access_token),
        json={}, timeout=30,
    )
    data = resp.json()
    if data.get("error", {}).get("code") != "ok":
        raise RuntimeError(f"creator_info query failed: {data}")
    return data.get("data", {})


def init_post(
    access_token: str, video_path: Path, caption: str,
    privacy_level: str, disable_comment: bool, disable_duet: bool,
    disable_stitch: bool, brand_content: bool, brand_organic: bool,
) -> tuple[str, str | None]:
    size = video_path.stat().st_size
    chunk_size = min(TIKTOK_UPLOAD_CHUNK_SIZE, size)
    total_chunks = max(1, (size + chunk_size - 1) // chunk_size)

    post_info: dict = {
        "title": caption,
        "privacy_level": privacy_level,
        "disable_comment": disable_comment,
        "disable_duet": disable_duet,
        "disable_stitch": disable_stitch,
    }
    if brand_content:
        post_info["brand_content_toggle"] = True
    if brand_organic:
        post_info["brand_organic_toggle"] = True

    body = {
        "post_info": post_info,
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }
    resp = requests.post(
        TIKTOK_VIDEO_INIT_URL, headers=_headers(access_token),
        json=body, timeout=30,
    )
    data = resp.json()
    if data.get("error", {}).get("code") != "ok":
        raise RuntimeError(f"video init failed: {data}")
    return data["data"]["publish_id"], data["data"].get("upload_url")


def upload_media(upload_url: str, video_path: Path) -> None:
    """PUT the video file to the upload_url using Content-Range chunking."""
    size = video_path.stat().st_size
    with open(video_path, "rb") as fh:
        offset = 0
        while offset < size:
            fh.seek(offset)
            chunk = fh.read(TIKTOK_UPLOAD_CHUNK_SIZE)
            end = min(size - 1, offset + len(chunk) - 1)
            headers = {
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes {offset}-{end}/{size}",
            }
            resp = requests.put(upload_url, headers=headers, data=chunk, timeout=600)
            if resp.status_code not in (200, 201, 204, 206):
                raise RuntimeError(
                    f"chunk upload failed (HTTP {resp.status_code}): {resp.text[:500]}"
                )
            offset += len(chunk)
            print(f"[tiktok]   uploaded {offset}/{size} bytes")


def fetch_post_status(access_token: str, publish_id: str) -> tuple[str, str]:
    resp = requests.post(
        TIKTOK_STATUS_URL, headers=_headers(access_token),
        json={"publish_id": publish_id}, timeout=30,
    )
    data = resp.json()
    if data.get("error", {}).get("code") != "ok":
        raise RuntimeError(f"status fetch failed: {data}")
    d = data.get("data", {})
    return d.get("status", ""), d.get("fail_reason", "")


# ──────────────────────────────────────────────
# INTERACTIVE COMPOSE
# ──────────────────────────────────────────────


def _video_duration(path: Path) -> float:
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", str(path),
        ], text=True)
        return float(json.loads(out).get("format", {}).get("duration", 0))
    except (subprocess.CalledProcessError, ValueError, json.JSONDecodeError):
        return 0.0


def _ask_interaction_toggle(label: str, account_disabled: bool) -> bool:
    """Ask one interaction prompt (default off), returning disable_*.

    When the account forces the toggle off, the disable flag is simply
    set true (honouring the account) without prompting.
    """
    if account_disabled:
        print(f"[tiktok]   ({label.lower()} disabled for this account)")
        return True
    allowed = questionary.confirm(f"Allow {label}?", default=False).ask()
    if allowed is None:
        sys.exit(0)
    return not allowed


def compose_and_publish(video_path: Path, dry_run: bool = False) -> None:
    creds = get_access_token()
    token = creds["access_token"]

    # 1) Query creator info — required before composing (UX guidelines).
    print(f"\n[tiktok] Querying creator info for open_id {creds['open_id']} ...")
    info = query_creator_info(token)
    creator = info.get("creator_username") or info.get("creator_nickname") or "creator"
    options = info.get("privacy_level_options") or ["SELF_ONLY"]
    max_dur = info.get("max_video_post_duration_sec")
    print(f"[tiktok] Account: {creator}")
    print(f"[tiktok] Allowed privacy levels: {', '.join(options)}")
    if max_dur:
        print(f"[tiktok] Max video duration: {max_dur}s")

    # 2) Privacy — must be one of the returned options; NO default.
    privacy = questionary.select("Privacy level:", choices=options).ask()
    if privacy is None:
        sys.exit(0)

    # 3) Interaction toggles — off by default, honour account settings.
    disable_comment = _ask_interaction_toggle("comments", info.get("is_comment_disabled") is True)
    disable_duet = _ask_interaction_toggle("duets", info.get("is_duet_disabled") is True)
    disable_stitch = _ask_interaction_toggle("stitches", info.get("is_stitch_disabled") is True)

    # 4) Caption — branded default, optionally edited, then previewed.
    default_caption = build_caption(video_path.stem)
    caption = default_caption
    if questionary.confirm("Edit the caption?", default=False).ask():
        edited = click.edit(default_caption)
        if edited and edited.strip():
            caption = edited.strip()
    print(f"\n{'=' * 64}\nCAPTION PREVIEW\n{'=' * 64}\n{caption}\n{'=' * 64}")

    # 5) Commercial disclosure — guidelines require an explicit choice.
    brand_content = bool(questionary.confirm(
        "Does this post promote a third-party commercial product?", default=False).ask())
    brand_organic = bool(questionary.confirm(
        "Does this post promote your own brand (RFLXN)?", default=False).ask())

    # 6) Video pre-checks vs the account's limits + demo size guidance.
    dur = _video_duration(video_path)
    size_mb = video_path.stat().st_size / (1024 * 1024)
    print(f"[video] {video_path.name} | {dur:.1f}s | {size_mb:.1f} MB")
    if size_mb > 50:
        print("[video] NOTE: larger than 50 MB — fine to post, but for the demo "
              "recording use a smaller clip so the video stays under 50 MB.")
    if max_dur and dur and dur > max_dur:
        print(f"[error] Video is {dur:.0f}s but the account allows {max_dur}s.")
        sys.exit(1)

    # 7) Explicit consent — required before publish.
    ok = bool(questionary.confirm(
        f'"{MUSIC_CONSENT_TEXT}". Publish now?', default=False).ask())
    if not ok:
        print("[abort] Not publishing.")
        sys.exit(0)

    if dry_run:
        print(f"[tiktok] DRY RUN — nothing uploaded. Caption + settings above are "
              f"what would post. Site link: {TIKTOK_SITE_BASE}")
        return

    # 8) Publish: init → upload → poll status.
    publish_id, upload_url = init_post(
        token, video_path, caption, privacy,
        disable_comment, disable_duet, disable_stitch,
        brand_content, brand_organic,
    )
    if upload_url:
        print("[tiktok] Uploading video ...")
        upload_media(upload_url, video_path)

    deadline = time.time() + TIKTOK_STATUS_TIMEOUT
    status, reason = "PROCESSING_UPLOAD", ""
    while status not in ("PUBLISH_COMPLETE", "FAILED") and time.time() < deadline:
        time.sleep(TIKTOK_STATUS_POLL_SEC)
        status, reason = fetch_post_status(token, publish_id)
        print(f"[tiktok]   status: {status}")

    if status == "PUBLISH_COMPLETE":
        print(f"[tiktok] Published ✓ ({publish_id})")
        print(f"[tiktok] Site link: {TIKTOK_SITE_BASE}")
    else:
        raise RuntimeError(f"TikTok publish failed ({status}): {reason}")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────


@click.command()
@click.option("--video", "-v", "video", type=click.Path(exists=True, path_type=Path),
              help="Path to the .mp4/.mov to publish (required to post)")
@click.option("--dry-run", is_flag=True, default=False,
              help="Run the full compose flow but skip the upload")
@click.option("--tiktok-auth", is_flag=True, default=False,
              help="Run the OAuth flow and exit")
def main(video: Path | None, dry_run: bool, tiktok_auth: bool) -> None:
    """Interactive, review-compliant TikTok post (Login Kit + Content Posting)."""
    if tiktok_auth:
        authorize_tiktok()
        return
    if video is None:
        raise click.UsageError("Missing --video (or use --tiktok-auth).")
    print(f"\n  TikTok Compose — {video}")
    compose_and_publish(video, dry_run=dry_run)


if __name__ == "__main__":
    main()