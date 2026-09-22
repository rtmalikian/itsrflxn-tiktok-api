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
export TIKTOK_REDIRECT_URI="http://localhost:8699"   # must match the portal

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

## 3. Repository layout

```
.
├── index.html                     # Landing page (links to both legal pages)
├── terms-of-service/index.html    # Terms of Service
├── privacy-policy/index.html      # Privacy Policy
├── tiktok-uploader/gen_rflxn.py   # Generator + TikTok posting bot
├── tiktok<TOKEN>.txt              # Site-verification signature file
└── README.md
```