# itsrflxn TikTok API

The home for **RFLXN's** TikTok development app: hosted legal pages for TikTok
Developer approval, plus the automated video-generation + TikTok posting bot.

- Artist: **RFLXN**
- Genres: **triphop & progressive melodic lofi**
- Location: **Palmdale, CA**
- Contact: **itsrflxn@gmail.com**

---

## 1. Legal pages (hosted on GitHub Pages)

These URLs are submitted to the TikTok Developer Portal for app approval:

| Page | URL |
| --- | --- |
| Terms of Service | https://rtmalikian.github.io/itsrflxn-tiktok-api/terms-of-service/ |
| Privacy Policy | https://rtmalikian.github.io/itsrflxn-tiktok-api/privacy-policy/ |

Both pages are plain static HTML with the branding already filled in (app name,
contact email). Edit `terms-of-service/index.html` / `privacy-policy/index.html`
and push to redeploy.

### Site verification for TikTok

TikTok asks you to prove you own the site before enabling `video.publish`.
The library file is placed at the repo root / `terms-of-service/` and served at:

```
https://rtmalikian.github.io/itsrflxn-tiktok-api/tiktok<TOKEN>.txt
```

To re-verify: download the new signature file from the TikTok portal, drop it
into this repo, delete the old one, and push. Verify with the GitHub Pages
prefix, **not** the `github.com/...` viewer URL.

---

## 2. tiktok-uploader — automated TikTok posting bot

`tiktok-uploader/gen_rflxn.py` is a fork of the `rm_shorts_gen` video generator
that adds an end-to-end TikTok publishing flow on top of the Content Posting
API v2 (Direct Post):

1. Generates 9:16 short reels and 16:9 long-form clips from a source video
   (Whisper word-level transcription → AI viral-segment detection → karaoke
   burned-in subtitles).
2. Writes a **branded caption** next to every clip (`.tiktok.txt`) that always
   includes the RFLXN music call-to-action, the triphop / progressive melodic
   lofi genre tagline, the Palmdale location, and hashtags.
3. Pushes every clip to TikTok — scheduling **one video per day** or posting
   immediately — with OAuth token refresh, chunked upload, and publish-status
   polling handled for you.

### Setup

```bash
# 1. Install the TikTok developer prerequisites
pip install faster-whisper click questionary requests

# 2. Credentials — either export them or let the flow prompt
export TIKTOK_CLIENT_KEY="..."
export TIKTOK_CLIENT_SECRET="..."
# Optional — defaults to the registered web redirect below
export TIKTOK_REDIRECT_URI="http://localhost:8699/callback/"

# 3. Authorise your TikTok account (runs the OAuth flow, saves ~/.itsrflxn_tiktok.json)
python3 tiktok-uploader/gen_rflxn.py --tiktok-auth
```

> The script auto-bootstraps a `.venv` in the directory you run it from when no
> virtualenv is active, then re-executes inside it.

### Daily scheduling (one post per day)

```bash
python3 gen_rflxn.py --input /path/to/source.mp4 --upload --mode schedule --watch
```

- Assigns tomorrow's slot to the first clip, then one clip per subsequent day.
- `--time 18:00` sets the daily slot (local time), `--interval-days N` spaces
  posts further apart.
- `--watch` keeps the process alive so it keeps posting as slots arrive; for a
  parked daemon, run without `--watch` after generation and call
  `--upload` later (it only enqueues not-already-queued clips).
- The queue is persisted in `output/.tiktok_schedule.json`.

### Post everything now

```bash
python3 gen_rflxn.py --input /path/to/source.mp4 --upload --mode immediate
```

### Other useful flags

| Flag | Purpose |
| --- | --- |
| `--privacy PUBLIC_TO_EVERYONE` | Post privacy level (must be offered by the account). |
| `--dry-run` | Print/schedule without uploading — safe to test. |
| `--no-longform` | Skip the 16:9 long-form clips, reels only. |
| `--whisper-model medium` | Whisper model size (tiny → large-v3). |
| `--force-transcribe` | Ignore the cached transcript. |
| `--debug` | Verbose FFmpeg / diagnostics. |

### Caption template

Every clip gets a deterministic, brand-consistent caption:

```
<AI hook line>

🚨 RFLXN is back with new sounds 🚨
Triphop & progressive melodic lofi — the RFLXN sound
Listen to RFLXN on ALL streaming platforms (Spotify, Apple Music, YouTube Music, and more)
You can also pick any RFLXN song as the sound for YOUR TikTok!

📍 Palmdale, CA

#RFLXN #RFLXNsounds #Triphop #TripHop #Lofi #MelodicLofi #ProgressiveLofi #LofiBeats
#NewMusic #MusicOnTikTok #StreamingNow #Palmdale #PalmdaleCA #California #MusicDrop
#ViralMusic #FYP #ForYou
```

Hashtags, the genre tagline, and the location are configurable at the top of
the file (`TIKTOK_HASHTAGS`, `GENRES`, `LOCATION`, `ARTIST_NAME`).

### Local video file → GitHub Pages URL

When a clip is generated or scheduled, the script also prints where the file
would live on this site (push the video into the repo under `videos/` to make
that URL real):

```
https://rtmalikian.github.io/itsrflxn-tiktok-api/videos/<clip-name>.mp4
```

`PULL_FROM_URL` publishing uses the same prefix, which is already verified in
the TikTok developer portal.

---

## 2b. tiktok_compose.py — interactive demo flow (for App Review)

`tiktok-uploader/tiktok_compose.py` is a small, TikTok-only companion that
walks through the exact flow TikTok's reviewers ask to see on screen for the
Content Posting API demo video:

1. **Login Kit** OAuth — `user.info.basic` + `video.publish`, local callback on
   port 8699.
2. **Creator Info query** — prints the account and *only* its allowed privacy
   options.
3. **Compose prompts** — privacy level (no default), comment/duet/stitch toggles
   (off by default, honouring account settings), editable RFLXN caption preview,
   and explicit commercial-disclosure questions.
4. **Consent** — "By posting, you agree to TikTok's Music Usage Confirmation".
5. **Publish** — `FILE_UPLOAD` upload with status polling to `PUBLISH_COMPLETE`.

### Redirect URIs registered in the portal

TikTok requires a **web redirect URI** (a real https domain) before review —
`localhost` alone is rejected with *"App must have web redirect uri or trusted
domain"*. This repo provides both:

| Category | URI | Path in repo | What it does |
| --- | --- | --- | --- |
| Web (Desktop) | `https://rtmalikian.github.io/itsrflxn-tiktok-api/callback/` | `callback/index.html` | Required for review. JS page forwards `code`+`state` to `http://localhost:8699/callback/`. |
| Web (Desktop) | `http://localhost:8699/callback/` | — | Direct loopback flow, useful while developing. |

Register both under **Login Kit → Redirect URIs**. The scripts always run the
loopback listener, so the GitHub Pages forward lands on it automatically.

### Recording the demo video

TikTok's review form accepts mp4/mov files, **up to 50 MB each**. Use two small
files, not one big one:

- **Test clip you post** — a short, low-bitrate mp4 (1–5 MB) so upload + polling
  finish fast and the recording stays short.
- **Screen recording** — ~1–2 minutes at 720p (macOS `Cmd+Shift+5`) showing:
  1. Opening `https://rtmalikian.github.io/itsrflxn-tiktok-api/` (the demo
     domain must match the Website URL submitted to TikTok).
  2. `python3 tiktok_compose.py --tiktok-auth` → the sandbox OAuth page → approve
     the scopes → redirect back to `localhost:8699/callback/`.
  3. `python3 tiktok_compose.py --video test_clip.mp4` → privacy selection,
     toggles, caption preview, consent, publish.
  4. Status polling reaching `PUBLISH_COMPLETE`, then the post visible in the
     TikTok app (private / `SELF_ONLY` for an unaudited app).

Sandbox note: sandbox mode does not offer Content Posting for **public** videos,
so demonstrate with `SELF_ONLY` and explain that `PUBLIC_TO_EVERYONE` is enabled
after approval.

---

## 3. Repository layout

```
.
├── index.html                     # Landing page (links to both legal pages)
├── terms-of-service/index.html    # Terms of Service
├── privacy-policy/index.html      # Privacy Policy
├── callback/index.html            # OAuth web redirect → local loopback bounce
├── tiktok-uploader/gen_rflxn.py   # Generator + TikTok posting bot
├── tiktok-uploader/tiktok_compose.py  # Interactive demo flow (App Review)
├── tiktok<TOKEN>.txt              # Site-verification signature file
└── README.md
```