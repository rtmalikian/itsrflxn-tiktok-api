#!/usr/bin/env python3
"""Automated Vertical Short-Form Video Generator (RFLXN edition).

Transforms wide horizontal videos into viral 9:16 shorts with
word-level karaoke subtitles, AI-selected highlight segments,
and blurred gradient background — then pushes every clip to TikTok
(short reels and long-form reels alike) via the TikTok Content
Posting API, scheduled one post per day, with a branded RFLXN
caption (music CTA + Palmdale location + hashtags).
"""

import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

# ──────────────────────────────────────────────
# VENV AUTO-BOOTSTRAP
# ──────────────────────────────────────────────

REQUIRED_PACKAGES = [
    "faster-whisper",
    "click",
    "questionary",
    "requests",
]

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
        subprocess.check_call(
            [sys.executable, "-m", "venv", str(VENV_DIR)]
        )
    else:
        print(f"[setup] Using existing virtual environment at {VENV_DIR}")

    pip = VENV_DIR / "bin" / "pip"
    print("[setup] Upgrading pip ...")
    subprocess.check_call([str(pip), "install", "--upgrade", "pip"])

    print("[setup] Installing required packages ...")
    for pkg in REQUIRED_PACKAGES:
        subprocess.check_call([str(pip), "install", pkg])

    # Re-execute ourselves inside the venv
    python = VENV_DIR / "bin" / "python3"
    if not python.exists():
        python = VENV_DIR / "bin" / "python"
    print(f"[setup] Re-executing inside venv: {python}")
    os.execv(str(python), [str(python), __file__, *sys.argv[1:]])


if not _in_venv():
    _bootstrap_venv()

# ---- imports that require installed packages ----
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote

import click
import questionary
import requests
from faster_whisper import WhisperModel

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────

import logging as _logging

logger = _logging.getLogger("gen")
_logging.basicConfig(
    level=_logging.INFO,
    format="%(levelname)-5s %(name)s | %(message)s",
)
# Logger is updated to DEBUG level when --debug is passed.

# ──────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────

MODEL_NAME = "opencode/big-pickle"
OUTPUT_DIR = Path.cwd() / "output"
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_FPS = 30
HOOK_DURATION = 4.0
PADDING = 1.5
BLUR_RADIUS = 50
BLUR_PASSES = 5
MAX_SEGMENTS = 5
MIN_SEGMENTS = 3
WHISPER_MODEL_SIZE = "medium"
OPENCODE_TIMEOUT = 600
AI_RETRIES = 2

# ──────────────────────────────────────────────
# SAFE ZONES (TikTok / IG Reels / YouTube Shorts, 1080x1920)
# ──────────────────────────────────────────────
# Published UI-overlay guidance for all three platforms converges on
# roughly the same numbers: keep clear of the top ~13% (search bar /
# For-You-Following tabs / channel icons) and the bottom ~22% (caption,
# username, sound title, CTA button, nav bar) plus a side gutter for the
# right-hand like/comment/share icon column.
SAFE_MARGIN_TOP = 250
SAFE_MARGIN_BOTTOM = 420
SAFE_MARGIN_SIDE = 64

SAFE_CONTENT_WIDTH = TARGET_WIDTH - 2 * SAFE_MARGIN_SIDE

# The hook is allowed to fill the upper safe band — from just below the
# top UI zone down to just under halfway down the frame — so it reads as
# a bold title card rather than a caption.
HOOK_MAX_HEIGHT = int(TARGET_HEIGHT * 0.42) - SAFE_MARGIN_TOP

# Karaoke captions live in the lower safe band, bottom-anchored above the
# reserved UI zone, capped so a long chunk can never grow into a wall of
# text the way the old single-block subtitle did.
KARAOKE_MAX_HEIGHT = int(TARGET_HEIGHT * 0.22)
KARAOKE_MAX_WORDS_PER_CHUNK = 4
KARAOKE_MAX_CHARS_PER_CHUNK = 20

# ──────────────────────────────────────────────
# MID-LENGTH LANDSCAPE CLIPS (16:9, for full monologues / motte-and-bailey
# rhetorical arcs — YouTube & Instagram feed, not Shorts/Reels)
# ──────────────────────────────────────────────
TARGET_WIDTH_H = 1920
TARGET_HEIGHT_H = 1080

# No reels-style app chrome to dodge here, just a normal video player —
# margins only need to clear a title-safe edge and native player controls
# (YouTube's CC button / progress bar, IG's caption area), so they're
# modest compared to the vertical safe zones above.
H_SAFE_MARGIN_TOP = 60
H_SAFE_MARGIN_BOTTOM = 90
H_SAFE_MARGIN_SIDE = 80
H_SAFE_CONTENT_WIDTH = TARGET_WIDTH_H - 2 * H_SAFE_MARGIN_SIDE
H_HOOK_MAX_HEIGHT = int(TARGET_HEIGHT_H * 0.30)
H_KARAOKE_MAX_HEIGHT = int(TARGET_HEIGHT_H * 0.16)

# Segment-detection tuning: monologues/rhetorical arcs run much longer
# than a viral soundbite and need a much bigger transcript window per
# scan chunk to see a whole arc at once.
MID_SCAN_CHUNK_WORDS = 1600
MID_SCAN_OVERLAP = 200
MID_MIN_SEGMENTS = 1
MID_MAX_SEGMENTS = 3
MID_MIN_DURATION = 90.0
MID_MAX_DURATION = 20 * 60.0  # sanity ceiling, not a target — "no fixed cap"

# A caption profile bundles everything build_ass_content/_fit_hook_text/
# _fit_karaoke_font need to know about the canvas they're rendering onto,
# so the exact same karaoke-style captioning logic serves both the
# vertical reels and the horizontal mid-length clips.
VERTICAL_PROFILE = {
    "width": TARGET_WIDTH, "height": TARGET_HEIGHT,
    "safe_top": SAFE_MARGIN_TOP, "safe_bottom": SAFE_MARGIN_BOTTOM, "safe_side": SAFE_MARGIN_SIDE,
    "hook_max_height": HOOK_MAX_HEIGHT, "karaoke_max_height": KARAOKE_MAX_HEIGHT,
    "hook_min_font": 110, "hook_max_font": 260,
    "karaoke_min_font": 56, "karaoke_max_font": 104,
}
HORIZONTAL_PROFILE = {
    "width": TARGET_WIDTH_H, "height": TARGET_HEIGHT_H,
    "safe_top": H_SAFE_MARGIN_TOP, "safe_bottom": H_SAFE_MARGIN_BOTTOM, "safe_side": H_SAFE_MARGIN_SIDE,
    "hook_max_height": H_HOOK_MAX_HEIGHT, "karaoke_max_height": H_KARAOKE_MAX_HEIGHT,
    "hook_min_font": 64, "hook_max_font": 140,
    "karaoke_min_font": 36, "karaoke_max_font": 64,
}

# ──────────────────────────────────────────────
# RFLXN BRAND & TIKTOK POSTING
# ──────────────────────────────────────────────

ARTIST_NAME = "RFLXN"
LOCATION = "Palmdale, CA"
GENRES = "triphop & progressive melodic lofi"

STREAMING_LINE = (
    f"Listen to {ARTIST_NAME} on ALL streaming platforms "
    f"(Spotify, Apple Music, YouTube Music, and more)"
)
MUSIC_TIKTOK_LINE = (
    f"You can also pick any {ARTIST_NAME} song as the sound for YOUR TikTok!"
)
GENRE_LINE = f"{GENRES.capitalize()} — the {ARTIST_NAME} sound"

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

# TikTok Content Posting API credentials. Set via environment variables or,
# after running `--tiktok-auth` once, they are persisted to a credentials file.
TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REDIRECT_URI = os.environ.get("TIKTOK_REDIRECT_URI", "http://localhost:8699")
TIKTOK_SCOPES = "user.info.basic,video.publish"
CREDENTIALS_FILE = Path.home() / ".itsrflxn_tiktok.json"

# GitHub Pages site that hosts the repo — clips pushed to the repo are
# reachable at these URLs (the site's prefix is verified in the TikTok
# developer portal, so PULL_FROM_URL can fetch them).
TIKTOK_SITE_BASE = "https://rtmalikian.github.io/itsrflxn-tiktok-api"
TIKTOK_VIDEO_DIR = "videos"

# Daily posting schedule — one video every day.
SCHEDULE_FILE = OUTPUT_DIR / ".tiktok_schedule.json"
DAILY_PUBLISH_TIME = "18:00"      # local time the daily post goes out
DAILY_INTERVAL_DAYS = 1           # one post every N days
TIKTOK_STATUS_POLL_SEC = 5        # how often to poll the publish status
TIKTOK_STATUS_TIMEOUT = 10 * 60   # give up polling after 10 minutes
TIKTOK_UPLOAD_CHUNK_SIZE = 64 * 1024 * 1024  # TikTok chunk ceiling (64 MiB)

# ──────────────────────────────────────────────
# FILLER-WORD TRIMMING
# ──────────────────────────────────────────────
# Unambiguous filler interjections — safe to always cut, they never carry
# meaning on their own.
FILLER_INTERJECTIONS = {"um", "umm", "uhm", "uh", "uhh", "erm", "er", "hmm", "mhm"}

# Riskier filler words/phrases — only cut with --aggressive-filler-trim,
# since e.g. "like" and "you know" are often meaningful ("I like that",
# "you know the answer") rather than verbal tics.
AGGRESSIVE_FILLER_WORDS = {"like"}
AGGRESSIVE_FILLER_PHRASES = [("you", "know"), ("i", "mean"), ("kind", "of"), ("sort", "of")]

# Word-level ASR timestamps are frequently tight around filler words —
# especially on the trailing edge, since a nasal "um"/"hmm" murmur trails
# off acoustically well after the token's reported end time. Pad each cut
# outward (clamped to neighboring words, so real speech is never eaten).
FILLER_PAD_BEFORE = 0.05
FILLER_PAD_AFTER = 0.18

# Short crossfade at each splice point so a hard sample-boundary cut never
# produces an audible click/pop.
FILLER_CUT_FADE = 0.02

# ──────────────────────────────────────────────
# FONTS & STYLING
# ──────────────────────────────────────────────

# Font families verified available via fc-list on macOS.
# Hook fonts are bold / display-oriented; karaoke fonts are clean / readable.
HOOK_FONT_CANDIDATES = [
    "Impact", "Arial Black", "Futura", "Trebuchet MS",
    "Verdana", "Georgia", "Gill Sans", "Optima",
    "Palatino", "Didot", "American Typewriter",
    "Baskerville", "Copperplate", "Chalkduster",
    "Noteworthy", "Rockwell", "Snell Roundhand",
    "Courier New", "Avenir", "Marker Felt",
]

KARAOKE_FONT_CANDIDATES = [
    "Helvetica", "Helvetica Neue", "Arial", "Verdana",
    "Trebuchet MS", "Gill Sans", "Futura",
    "Optima", "Avenir", "Georgia",
    "Palatino", "Cochin", "Baskerville",
    "American Typewriter", "Didot", "Menlo",
    "Courier New", "Geneva", "Lucida Grande",
]

# Color-blind-safe palette (Okabe-Ito inspired).
# ASS color format: &HAABBGGRR  (alpha, blue, green, red).
TEXT_COLORS = {
    "white":      "&H00FFFFFF",
    "yellow":     "&H0044E2F0",  # #F0E442
    "orange":     "&H00009FE6",  # #E69F00
    "vermillion": "&H00005ED5",  # #D55E00
    "sky_blue":   "&H00E9B456",  # #56B4E9
    "blue":       "&H00B27200",  # #0072B2
    "pink":       "&H00A779CC",  # #CC79A7
    "green":      "&H00739E00",  # #009E73
}

BOX_COLORS = {
    "dark":      "&H88000000",
    "charcoal":  "&H88333333",
    "blue_tint": "&H88554433",
    "warm":      "&H88332233",
    "deep":      "&H88442244",
}

_AVAILABLE_HOOK_FONTS: list[str] | None = None
_AVAILABLE_KARAOKE_FONTS: list[str] | None = None


def _probe_available_fonts() -> tuple[list[str], list[str]]:
    """Use fc-list to discover which curated fonts exist on this system.

    Normalises comma-separated family entries (common on macOS fontconfig)
    so that e.g. "Avenir" matches a line like "Avenir,Avenir Black".
    Returns (hook_fonts, karaoke_fonts) filtered to what's available.
    """
    try:
        result = subprocess.run(
            ["fc-list", "--format=%{family}\\n"],
            capture_output=True, text=True, timeout=10,
        )
        present: set[str] = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            for part in line.split(","):
                part = part.strip()
                if part:
                    present.add(part)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        present = set()

    if not present:
        return list(HOOK_FONT_CANDIDATES), list(KARAOKE_FONT_CANDIDATES)

    def intersect(candidates: list[str]) -> list[str]:
        matched = [f for f in candidates if f in present]
        if not matched:
            matched = [f for f in candidates if f.split()[0] in {p.split()[0] for p in present}]
        return matched or ["Arial"]

    return intersect(HOOK_FONT_CANDIDATES), intersect(KARAOKE_FONT_CANDIDATES)


def available_fonts() -> tuple[list[str], list[str]]:
    global _AVAILABLE_HOOK_FONTS, _AVAILABLE_KARAOKE_FONTS
    if _AVAILABLE_HOOK_FONTS is None:
        _AVAILABLE_HOOK_FONTS, _AVAILABLE_KARAOKE_FONTS = _probe_available_fonts()
    return _AVAILABLE_HOOK_FONTS, _AVAILABLE_KARAOKE_FONTS


def random_style_config() -> dict:
    """Return a random set of styling parameters for one segment."""
    hook_fonts, karaoke_fonts = available_fonts()

    # --- hook style ---
    hook_font = random.choice(hook_fonts)
    hook_color = random.choice(list(TEXT_COLORS.values()))
    hook_box = random.choice(list(BOX_COLORS.values()))
    hook_bold = True  # always bold for impact
    hook_italic = random.choice([True, False])
    # 70 % chance of background box, 30 % heavy outline
    hook_border_style = random.choices([3, 1], weights=[70, 30])[0]

    # --- karaoke style ---
    karaoke_font = random.choice(karaoke_fonts)
    # For karaoke keep primary bright for readability
    karaoke_primary = random.choice([
        "&H00FFFFFF",  # white
        "&H0044E2F0",  # yellow
        "&H00E9B456",  # sky blue
        "&H00B27200",  # blue
    ])
    karaoke_secondary = random.choice([
        "&H00666666",  # medium gray
        "&H00777777",  # lighter gray
        "&H00554433",  # muted blue-gray
    ])
    karaoke_box = random.choice(list(BOX_COLORS.values()))
    karaoke_bold = random.choice([True, False])
    karaoke_italic = random.choice([True, False])
    karaoke_border_style = 3  # always background box for readability

    return {
        "hook": {
            "font": hook_font,
            "color": hook_color,
            "box": hook_box,
            "bold": hook_bold,
            "italic": hook_italic,
            "border_style": hook_border_style,
        },
        "karaoke": {
            "font": karaoke_font,
            "primary": karaoke_primary,
            "secondary": karaoke_secondary,
            "box": karaoke_box,
            "bold": karaoke_bold,
            "italic": karaoke_italic,
            "border_style": karaoke_border_style,
        },
    }

# ──────────────────────────────────────────────
# FFMPEG PRECHECKS
# ──────────────────────────────────────────────


def _check_ffmpeg_libass() -> None:
    """Verify FFmpeg has the subtitles filter (libass)."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-filters"],
            capture_output=True, text=True, timeout=15,
        )
        if "subtitles" not in result.stdout:
            logger.warning("FFmpeg missing 'subtitles' filter — subtitles will not render")
        else:
            logger.debug("FFmpeg libass (subtitles filter) available")
    except FileNotFoundError:
        logger.error("ffmpeg binary not found on PATH")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg filter probe timed out — proceeding anyway")


# ──────────────────────────────────────────────
# UTILITY HELPERS
# ──────────────────────────────────────────────


def fmt_time(seconds: float) -> str:
    """Format seconds to ASS time format H:MM:SS.cs"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def slugify(text: str, max_len: int = 40) -> str:
    """Turn text into a safe filesystem fragment."""
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "_", slug.strip())[:max_len]
    return slug.strip("_")


def get_video_info(path: Path) -> dict:
    """Return duration (sec), width, height, fps of a video via ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.check_output(cmd, text=True)
    data = json.loads(result)

    duration = float(data.get("format", {}).get("duration", 0))
    width = height = 0
    fps = TARGET_FPS
    for stream in data.get("streams", []):
        if stream["codec_type"] == "video":
            width = stream.get("width", 0)
            height = stream.get("height", 0)
            avg = stream.get("avg_frame_rate", "30/1")
            if "/" in avg:
                num, den = avg.split("/")
                fps = round(float(num) / float(den)) if float(den) != 0 else TARGET_FPS
            break

    return {"duration": duration, "width": width, "height": height, "fps": fps}


# ──────────────────────────────────────────────
# TRANSCRIPT CACHE
# ──────────────────────────────────────────────


def _transcript_cache_path(source: Path) -> Path:
    """Deterministic cache filename based on file identity (path + mtime + size)."""
    stat = source.stat()
    # Build a short hash from path, mtime (seconds), and file size
    raw = f"{source.resolve()}|{int(stat.st_mtime)}|{stat.st_size}"
    sig = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return OUTPUT_DIR / f"{source.stem}_{sig}_transcript.json"


def save_transcript_cache(words: list[dict], source: Path) -> Path:
    """Persist word-level timestamps to a JSON cache file."""
    cache = _transcript_cache_path(source)
    data = {"words": words, "source": str(source.resolve()), "cached_at": time.time()}
    cache.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.debug("Transcript cache saved: %s", cache)
    return cache


def load_transcript_cache(source: Path) -> list[dict] | None:
    """Return cached words if cache file exists and source hasn't changed."""
    cache = _transcript_cache_path(source)
    if not cache.exists():
        return None

    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt cache file — ignoring: %s", cache)
        return None

    # Quick sanity — must have words
    words = data.get("words", [])
    if not words:
        return None

    print(f"[cache] Found cached transcript ({len(words)} words) — {cache.name}")
    return words


# ──────────────────────────────────────────────
# AI HELPERS
# ──────────────────────────────────────────────


def call_opencode(prompt: str) -> str:
    """Send a prompt to opencode CLI and return the text reply."""
    cmd = [
        "opencode", "run",
        "--model", MODEL_NAME,
        "--format", "json",
    ]
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=OPENCODE_TIMEOUT,
    )

    if proc.returncode != 0:
        raise RuntimeError(f"opencode exited with code {proc.returncode}: {proc.stderr}")

    output = ""
    for line in proc.stdout.strip().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "text":
            output += event.get("part", {}).get("text", "")

    if not output:
        raise RuntimeError("opencode returned empty response")

    return output.strip()


# ──────────────────────────────────────────────
# STAGE 1 — INTERACTIVE INGESTION
# ──────────────────────────────────────────────


def pick_video(preselected: Path | None = None) -> Path:
    """Return a valid video file path — from *preselected* (--input) if
    given and valid, otherwise via interactive prompt."""
    if preselected is not None:
        if not preselected.is_absolute():
            print(f"[error] --input must be an absolute path: {preselected}")
            sys.exit(1)
        if not preselected.exists():
            print(f"[error] File not found: {preselected}")
            sys.exit(1)
        if preselected.suffix.lower() not in (".mp4", ".mkv", ".mov"):
            print(f"[error] Unsupported format: {preselected.suffix}. Use .mp4, .mkv, or .mov.")
            sys.exit(1)
        return preselected

    while True:
        raw = questionary.text(
            "Enter absolute path to source video (.mp4 / .mkv / .mov):",
        ).ask()
        if raw is None:
            print("[abort] User cancelled.")
            sys.exit(0)

        path = Path(raw.strip())
        if not path.is_absolute():
            print("[error] Please provide an absolute path.")
            continue
        if not path.exists():
            print(f"[error] File not found: {path}")
            continue
        if path.suffix.lower() not in (".mp4", ".mkv", ".mov"):
            print(f"[error] Unsupported format: {path.suffix}. Use .mp4, .mkv, or .mov.")
            continue
        return path


# ──────────────────────────────────────────────
# STAGE 2 — WHISPER TRANSCRIPTION
# ──────────────────────────────────────────────


def transcribe(video_path: Path) -> list[dict]:
    """Run Whisper and return word-level segments.

    Each entry: { "word": str, "start": float, "end": float }
    """
    print(f"[whisper] Loading model '{WHISPER_MODEL_SIZE}' (first run downloads) ...")
    model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")

    print(f"[whisper] Transcribing {video_path.name} ...")
    # Whisper is trained to produce "clean" transcripts and will often
    # silently smooth over um/uh/erm disfluencies rather than transcribing
    # them — with no literal "um" token, filler-word trimming has nothing
    # to match. An initial_prompt written in a filler-heavy style biases
    # the decoder toward transcribing them verbatim instead of dropping
    # them (a well-known Whisper technique; not 100% reliable, but the
    # only lever faster-whisper exposes for this).
    filler_prompt = (
        "Um, uh, so, like, you know, I mean — this is a casual, unscripted "
        "conversation with natural filler words, false starts, and pauses."
    )
    segments, info = model.transcribe(
        str(video_path), language="en", word_timestamps=True,
        initial_prompt=filler_prompt,
    )
    print(f"[whisper] Detected language: {info.language} (prob: {info.language_probability:.2f})")

    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                words.append({
                    "word": w.word.strip(),
                    "start": w.start,
                    "end": w.end,
                })

    if not words:
        raise RuntimeError("Whisper returned no words. Ensure the video has English speech.")

    print(f"[whisper] Extracted {len(words)} words.")
    return words


def _format_words_for_prompt(words: list[dict]) -> str:
    """Format word timestamps for an AI prompt, verbatim (no sampling)."""
    lines = [f"{w['word']} [{w['start']:.2f}s - {w['end']:.2f}s]" for w in words]
    chunk_size = 60
    chunks = ["\n".join(lines[i:i + chunk_size]) for i in range(0, len(lines), chunk_size)]
    return "\n---\n".join(chunks)


def _extract_json_array(raw: str) -> list:
    json_match = re.search(r"\[[\s\S]*\]", raw)
    if not json_match:
        raise RuntimeError(f"AI response did not contain a JSON array.\nResponse:\n{raw[:500]}")
    data = json.loads(json_match.group())
    if not isinstance(data, list):
        raise RuntimeError("AI returned JSON that was not a list")
    return data


def _call_ai_for_json_array(prompt: str, label: str) -> list:
    """Call opencode and parse a JSON array from the response, retrying on failure."""
    last_error = None
    for attempt in range(1, AI_RETRIES + 1):
        try:
            raw = call_opencode(prompt)
            return _extract_json_array(raw)
        except (RuntimeError, json.JSONDecodeError) as e:
            last_error = RuntimeError(f"{label}: {e}")
            if attempt < AI_RETRIES:
                print(f"[ai] {label} attempt {attempt} failed, retrying ...")
                time.sleep(2)
    raise last_error or RuntimeError(f"{label} failed after retries.")


# ──────────────────────────────────────────────
# STAGE 3 — AI SEGMENT DETECTION (full-transcript map-reduce scan)
# ──────────────────────────────────────────────
#
# Rather than down-sampling a long transcript to fit one prompt (which
# means the AI never even sees most of the video), the transcript is
# scanned word-for-word in overlapping chunks small enough to fit
# comfortably in a single prompt. Each chunk yields its own scored
# candidates; the candidates are then deduped and — if there are more
# than fit in one short-form batch — ranked in a final, much smaller
# prompt that only has to compare candidates, not re-read the transcript.

SCAN_CHUNK_WORDS = 450
SCAN_CHUNK_OVERLAP = 40


def _scan_chunks_sized(words: list[dict], chunk_words: int, overlap: int) -> list[list[dict]]:
    """Split the full word list into overlapping chunks so every word is
    scanned — long transcripts are never down-sampled."""
    if len(words) <= chunk_words:
        return [words]

    chunks = []
    step = chunk_words - overlap
    i = 0
    while True:
        chunks.append(words[i:i + chunk_words])
        if i + chunk_words >= len(words):
            break
        i += step
    return chunks


def _scan_chunks(words: list[dict]) -> list[list[dict]]:
    return _scan_chunks_sized(words, SCAN_CHUNK_WORDS, SCAN_CHUNK_OVERLAP)


def _detect_candidates_in_chunk(chunk_words: list[dict], chunk_idx: int, total_chunks: int) -> list[dict]:
    """Ask the AI for viral-clip candidates within a single transcript chunk."""
    transcript_text = _format_words_for_prompt(chunk_words)
    prompt = textwrap.dedent(f"""\
    You are a viral content analyst scanning part {chunk_idx}/{total_chunks} of a longer
    transcript for short-form clip potential. Identify up to 3 moments in THIS excerpt
    that would perform well as standalone viral short-form clips — funny, engaging,
    surprising, or controversial.

    Return ONLY a valid JSON array of objects (an empty array [] if nothing stands out).
    Each object MUST have:
    - "text": the exact text of the moment (verbatim from transcript)
    - "start_time": start time in seconds (number)
    - "end_time": end time in seconds (number)
    - "score": how viral this moment is, 1-10 (number)

    Rules:
    - Every moment duration (end_time - start_time) MUST be under 59 seconds.
    - Only flag moments that feel complete and compelling on their own.
    - Timestamps refer to the word-level timing shown below.

    Transcript excerpt with word-level timestamps:
    {transcript_text}
    """)

    try:
        data = _call_ai_for_json_array(prompt, f"chunk {chunk_idx}/{total_chunks} scan")
    except RuntimeError as e:
        print(f"[ai]   chunk {chunk_idx}/{total_chunks} scan failed, skipping: {e}")
        return []

    candidates = []
    for item in data:
        try:
            start = float(item["start_time"])
            end = float(item["end_time"])
            text = str(item["text"])
            score = float(item.get("score", 5))
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start or end - start > 59:
            continue
        candidates.append({"text": text, "start_time": start, "end_time": end, "score": score})
    return candidates


def _dedupe_candidates(candidates: list[dict]) -> list[dict]:
    """Merge candidates that substantially overlap (typically the same
    moment re-detected from two overlapping scan chunks), keeping the
    higher-scored one."""
    candidates = sorted(candidates, key=lambda c: c["start_time"])
    merged: list[dict] = []
    for c in candidates:
        if merged:
            prev = merged[-1]
            overlap = min(prev["end_time"], c["end_time"]) - max(prev["start_time"], c["start_time"])
            shorter = min(c["end_time"] - c["start_time"], prev["end_time"] - prev["start_time"])
            if overlap > 0 and shorter > 0 and overlap / shorter > 0.5:
                if c["score"] > prev["score"]:
                    merged[-1] = c
                continue
        merged.append(c)
    return merged


def _rank_candidates(
    candidates: list[dict], min_count: int, max_count: int, style: str = "viral short-form content"
) -> list[dict]:
    """Final pass: ask the AI to pick the best non-overlapping candidates
    from the (already small) deduped candidate list."""
    listing = "\n".join(
        f"{i + 1}. [{c['start_time']:.2f}s-{c['end_time']:.2f}s] (score {c['score']:.0f}/10) {c['text'][:200]}"
        for i, c in enumerate(candidates)
    )
    prompt = textwrap.dedent(f"""\
    You are a content analyst. Below are candidate moments already pulled from a full
    word-for-word transcript scan. Pick the {min_count}-{max_count} best, non-overlapping
    candidates for {style}, prioritizing variety and standalone impact.

    Return ONLY a valid JSON array of the chosen candidates' 1-based numbers, e.g. [2,5,7].

    Candidates:
    {listing}
    """)

    try:
        data = _call_ai_for_json_array(prompt, "final ranking")
        chosen = []
        for n in data:
            idx = int(n) - 1
            if 0 <= idx < len(candidates):
                chosen.append(candidates[idx])
        if chosen:
            return chosen[:max_count]
    except (RuntimeError, TypeError, ValueError) as e:
        print(f"[ai] Final ranking failed ({e}); falling back to score-based selection.")

    # Fallback: greedy score-sorted, non-overlapping selection.
    ranked = sorted(candidates, key=lambda c: c["score"], reverse=True)
    picked: list[dict] = []
    for c in ranked:
        if all(c["end_time"] <= p["start_time"] or c["start_time"] >= p["end_time"] for p in picked):
            picked.append(c)
        if len(picked) >= max_count:
            break
    return picked


def detect_viral_segments(
    words: list[dict], video_duration: float
) -> list[dict]:
    """Scan the full transcript word-for-word (in chunks) to identify
    3-5 viral segments — no down-sampling, so nothing is skipped.

    Returns list of { "text": str, "start_time": float, "end_time": float }
    """
    chunks = _scan_chunks(words)
    print(f"[ai] Scanning full transcript word-for-word in {len(chunks)} chunk(s) "
          f"({len(words)} words total) ...")

    all_candidates: list[dict] = []
    for i, chunk in enumerate(chunks, 1):
        print(f"[ai]   chunk {i}/{len(chunks)} ({len(chunk)} words) ...")
        all_candidates.extend(_detect_candidates_in_chunk(chunk, i, len(chunks)))

    if not all_candidates:
        raise RuntimeError("AI found no viral candidates across the full transcript scan.")

    candidates = _dedupe_candidates(all_candidates)
    print(f"[ai] {len(all_candidates)} raw candidate(s) -> {len(candidates)} after dedup.")

    chosen = (
        _rank_candidates(candidates, MIN_SEGMENTS, MAX_SEGMENTS)
        if len(candidates) > MAX_SEGMENTS else candidates
    )

    validated = []
    for seg in chosen[:MAX_SEGMENTS]:
        start = seg["start_time"]
        end = seg["end_time"]
        duration = end - start
        if duration > 59:
            end = start + 59
        validated.append({
            "text": seg["text"],
            "start_time": max(0, start - PADDING),
            "end_time": min(video_duration, end + PADDING),
        })

    validated.sort(key=lambda x: x["start_time"])
    cleaned = []
    for seg in validated:
        if not cleaned or seg["start_time"] - cleaned[-1]["end_time"] > 1.0:
            cleaned.append(seg)

    print(f"[ai] Selected {len(cleaned)} segment(s) from full-transcript scan.")
    for i, seg in enumerate(cleaned, 1):
        dur = seg["end_time"] - seg["start_time"]
        print(f"       {i}. {dur:.1f}s — \"{seg['text'][:60]}...\"")

    return cleaned


# ──────────────────────────────────────────────
# STAGE 3b — AI LONG-FORM SEGMENT DETECTION (monologues / motte-and-bailey)
# ──────────────────────────────────────────────
#
# Same map-reduce word-for-word scanning approach as the short-form scan
# above, but tuned for multi-minute, self-contained rhetorical arcs rather
# than punchy soundbites: much bigger scan-chunk window (so a whole
# monologue is visible to the AI in one prompt), a duration floor well
# above what a reel could hold, and a prompt asking for structural
# completeness instead of virality.


def _detect_longform_candidates_in_chunk(
    chunk_words: list[dict], chunk_idx: int, total_chunks: int
) -> list[dict]:
    """Ask the AI for complete monologue / motte-and-bailey candidates
    within a single (large) transcript chunk."""
    transcript_text = _format_words_for_prompt(chunk_words)
    prompt = textwrap.dedent(f"""\
    You are a long-form content editor scanning part {chunk_idx}/{total_chunks} of a longer
    livestream transcript for complete, self-contained passages that work as standalone
    mid-length YouTube/Instagram videos — NOT quick soundbites. Look specifically for:
    - A complete monologue or story with a clear beginning, middle, and payoff/conclusion.
    - "Motte-and-bailey" argument structures: a bold/contentious claim advanced, then
      retreated to a narrower, more defensible version of it when challenged (or the
      reverse). Flag the FULL arc from the initial claim through the retreat/resolution,
      not just a fragment of it.

    Return ONLY a valid JSON array of objects (an empty array [] if nothing in this excerpt
    qualifies). Each object MUST have:
    - "text": the exact text of the passage (verbatim from transcript)
    - "start_time": start time in seconds (number)
    - "end_time": end time in seconds (number)
    - "score": how complete/compelling this passage is as a standalone long-form clip, 1-10

    Rules:
    - Every passage duration (end_time - start_time) MUST be at least {MID_MIN_DURATION:.0f}
      seconds and under {MID_MAX_DURATION:.0f} seconds.
    - Only flag passages that feel complete and don't cut off mid-thought.
    - Timestamps refer to the word-level timing shown below.

    Transcript excerpt with word-level timestamps:
    {transcript_text}
    """)

    try:
        data = _call_ai_for_json_array(prompt, f"long-form chunk {chunk_idx}/{total_chunks} scan")
    except RuntimeError as e:
        print(f"[ai]   long-form chunk {chunk_idx}/{total_chunks} scan failed, skipping: {e}")
        return []

    candidates = []
    for item in data:
        try:
            start = float(item["start_time"])
            end = float(item["end_time"])
            text = str(item["text"])
            score = float(item.get("score", 5))
        except (KeyError, TypeError, ValueError):
            continue
        if not (MID_MIN_DURATION <= end - start <= MID_MAX_DURATION):
            continue
        candidates.append({"text": text, "start_time": start, "end_time": end, "score": score})
    return candidates


def detect_longform_segments(words: list[dict], video_duration: float) -> list[dict]:
    """Scan the full transcript word-for-word for complete monologues /
    motte-and-bailey rhetorical arcs suitable as mid-length clips.

    Unlike the reel scan, there's no fixed target length — the AI marks
    the natural start/end of the passage, bounded only by MID_MIN_DURATION
    and a generous MID_MAX_DURATION sanity ceiling. Returns [] (not an
    error) if nothing qualifies — mid-length clips are a bonus output,
    not the main deliverable.
    """
    chunks = _scan_chunks_sized(words, MID_SCAN_CHUNK_WORDS, MID_SCAN_OVERLAP)
    print(f"[ai] Scanning full transcript for long-form monologues/motte-bailey arcs "
          f"in {len(chunks)} chunk(s) ({len(words)} words total) ...")

    all_candidates: list[dict] = []
    for i, chunk in enumerate(chunks, 1):
        print(f"[ai]   long-form chunk {i}/{len(chunks)} ({len(chunk)} words) ...")
        all_candidates.extend(_detect_longform_candidates_in_chunk(chunk, i, len(chunks)))

    if not all_candidates:
        print("[ai] No long-form monologue/motte-bailey candidates found.")
        return []

    candidates = _dedupe_candidates(all_candidates)
    print(f"[ai] {len(all_candidates)} raw long-form candidate(s) -> {len(candidates)} after dedup.")

    chosen = (
        _rank_candidates(candidates, MID_MIN_SEGMENTS, MID_MAX_SEGMENTS, style="mid-length long-form clips")
        if len(candidates) > MID_MAX_SEGMENTS else candidates
    )

    validated = []
    for seg in chosen[:MID_MAX_SEGMENTS]:
        start = seg["start_time"]
        end = seg["end_time"]
        validated.append({
            "text": seg["text"],
            "start_time": max(0, start - PADDING),
            "end_time": min(video_duration, end + PADDING),
        })

    validated.sort(key=lambda x: x["start_time"])
    cleaned = []
    for seg in validated:
        if not cleaned or seg["start_time"] - cleaned[-1]["end_time"] > 1.0:
            cleaned.append(seg)

    print(f"[ai] Selected {len(cleaned)} long-form segment(s).")
    for i, seg in enumerate(cleaned, 1):
        dur = seg["end_time"] - seg["start_time"]
        print(f"       {i}. {dur / 60:.1f}min — \"{seg['text'][:60]}...\"")

    return cleaned


def _strip_hook_wrapping(text: str) -> str:
    """Strip markdown emphasis markers and straight/smart quotes the AI
    sometimes wraps a hook in (e.g. `**"Like this"**`), repeatedly from
    both ends until nothing more comes off."""
    wrapper_chars = "*_\"'“”‘’`"
    prev = None
    while prev != text:
        prev = text
        text = text.strip().strip(wrapper_chars)
    return text


def generate_hook(segment_text: str) -> str:
    """Use opencode to generate an engaging hook for a segment."""
    prompt = textwrap.dedent(f"""\
    Generate a high-impact text hook (maximum 6-10 words) for this video clip.
    The hook should make viewers stop scrolling.
    Return ONLY the hook text, nothing else.

    Clip content: {segment_text}
    """)

    print("[ai] Generating hook ...")
    last_error = None
    for attempt in range(1, AI_RETRIES + 1):
        try:
            raw = call_opencode(prompt)
        except RuntimeError as e:
            last_error = e
            if attempt < AI_RETRIES:
                print(f"[ai] Hook attempt {attempt} failed, retrying ...")
                time.sleep(1)
            continue

        hook = _strip_hook_wrapping(raw)
        hook = re.sub(r"\s+", " ", hook)
        words = hook.split()
        if len(words) > 10:
            hook = " ".join(words[:10])
        if hook:
            return hook
        last_error = RuntimeError("Hook was empty")
        if attempt < AI_RETRIES:
            print(f"[ai] Hook attempt {attempt} empty, retrying ...")
            time.sleep(1)

    raise last_error or RuntimeError("Hook generation failed.")


def generate_metadata(segment_text: str, hook: str) -> dict:
    """Use opencode to generate YouTube title/desc/tags and social caption."""
    prompt = textwrap.dedent(f"""\
    You are a social media content strategist. Given a video clip transcript and its hook text,
    generate the following in valid JSON (no markdown, no code fences).

    Return a JSON object with these keys:
      "youtube_title" — catchy YouTube #Shorts title (under 100 chars)
      "youtube_description" — 1-2 sentence description with a CTA (under 500 chars)
      "youtube_tags" — array of 8-15 comma-free tag strings relevant to the content
      "social_caption" — TikTok / Instagram Reels caption with 3-5 relevant hashtags (under 300 chars)

    Transcript of clip: {segment_text}
    Hook on screen: {hook}
    """)

    print("[ai] Generating YouTube + social metadata ...")
    last_error = None
    for attempt in range(1, AI_RETRIES + 1):
        try:
            raw = call_opencode(prompt)
        except RuntimeError as e:
            last_error = e
            if attempt < AI_RETRIES:
                print(f"[ai] Metadata attempt {attempt} failed, retrying ...")
                time.sleep(1)
            continue

        # Strip markdown fences if present
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            last_error = RuntimeError("AI returned invalid JSON")
            if attempt < AI_RETRIES:
                print(f"[ai] Metadata attempt {attempt} bad JSON, retrying ...")
                time.sleep(1)
            continue

        required = {"youtube_title", "youtube_description", "youtube_tags", "social_caption"}
        if not required.issubset(data.keys()):
            missing = required - data.keys()
            last_error = RuntimeError(f"Missing keys: {missing}")
            if attempt < AI_RETRIES:
                print(f"[ai] Metadata attempt {attempt} missing {missing}, retrying ...")
                time.sleep(1)
            continue

        if isinstance(data["youtube_tags"], list) and data["youtube_tags"]:
            return data

        last_error = RuntimeError("youtube_tags empty or not a list")
        if attempt < AI_RETRIES:
            print(f"[ai] Metadata attempt {attempt} empty tags, retrying ...")
            time.sleep(1)

    raise last_error or RuntimeError("Metadata generation failed after retries.")


# ──────────────────────────────────────────────
# STAGE 4 — SUBTITLE GENERATION (ASS)
# ──────────────────────────────────────────────


def _balance_lines(words: list[str], n_lines: int) -> list[str]:
    """Distribute words across exactly n_lines, balanced by character count."""
    if n_lines <= 1 or len(words) <= n_lines:
        # One word per line once we have more lines than words.
        if len(words) <= n_lines:
            return words
        n_lines = 1

    total_chars = sum(len(w) for w in words)
    target = total_chars / n_lines

    lines: list[list[str]] = []
    cur: list[str] = []
    cur_chars = 0
    for w in words:
        if cur and cur_chars + len(w) > target * 1.15 and len(lines) < n_lines - 1:
            lines.append(cur)
            cur = [w]
            cur_chars = len(w)
        else:
            cur.append(w)
            cur_chars += len(w)
    if cur:
        lines.append(cur)

    return [" ".join(l) for l in lines]


def _fit_hook_text(
    text: str,
    profile: dict = VERTICAL_PROFILE,
    avg_char_width: float = 0.62,
    line_spacing: float = 1.18,
    max_lines: int = 5,
) -> tuple[str, int]:
    """Auto-fit hook text: pick the line split + font size that maximises
    font size while fitting inside the upper safe-zone box of *profile*.

    Returns (ass_display_text, font_size).
    """
    max_width = profile["width"] - 2 * profile["safe_side"]
    max_height = profile["hook_max_height"]
    min_font = profile["hook_min_font"]
    max_font = profile["hook_max_font"]

    words = text.split()
    if not words:
        return text, min_font

    best_font = 0.0
    best_lines: list[str] = [text]
    for n_lines in range(1, min(max_lines, len(words)) + 1):
        lines = _balance_lines(words, n_lines)
        longest = max(len(l) for l in lines)
        font_by_width = max_width / (longest * avg_char_width + 1)
        font_by_height = max_height / (len(lines) * line_spacing)
        font = min(font_by_width, font_by_height, max_font)
        if font > best_font:
            best_font = font
            best_lines = lines

    font_size = int(max(min_font, min(max_font, best_font)))
    return "\\N".join(best_lines), font_size


def _build_karaoke_chunks(
    words: list[dict],
    max_words: int = KARAOKE_MAX_WORDS_PER_CHUNK,
    max_chars: int = KARAOKE_MAX_CHARS_PER_CHUNK,
) -> list[list[dict]]:
    """Group words into short bursts for karaoke display.

    Bounded by both word count and character budget so a chunk always
    renders on a single short line — never the wall-of-text a purely
    word-count-based split can produce with long words.
    """
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for w in words:
        wl = len(w["word"])
        if cur and (len(cur) >= max_words or cur_chars + wl + 1 > max_chars):
            chunks.append(cur)
            cur = [w]
            cur_chars = wl
        else:
            cur.append(w)
            cur_chars += wl + 1
    if cur:
        chunks.append(cur)

    # Merge a trailing single-word chunk into the previous one so a lone
    # word never flashes by itself. (`chunks[-2] = chunks[-2] + chunks.pop()`
    # is a classic evaluation-order trap: pop() mutates the list before the
    # `-2` target index is resolved, so it clobbers the wrong element.)
    if len(chunks) > 1 and len(chunks[-1]) == 1:
        last = chunks.pop()
        chunks[-1] = chunks[-1] + last

    return chunks


def _fit_karaoke_font(
    chunk_text: str,
    profile: dict = VERTICAL_PROFILE,
    avg_char_width: float = 0.58,
) -> int:
    """Estimate the best font size so a karaoke chunk fills the width
    without overflowing the lower safe-zone band of *profile*."""
    max_width = profile["width"] - 2 * profile["safe_side"]
    max_height = profile["karaoke_max_height"]
    min_font = profile["karaoke_min_font"]
    max_font = profile["karaoke_max_font"]
    font_by_width = max_width / (len(chunk_text) * avg_char_width + 1)
    font_by_height = max_height / 1.2  # chunks are always a single line
    return int(max(min_font, min(max_font, font_by_width, font_by_height)))


def _style_line(name: str, font: str, size: int, primary: str, secondary: str,
                 box: str, bold: int, italic: int, border: int, margin_v: int,
                 margin_l: int = SAFE_MARGIN_SIDE, margin_r: int = SAFE_MARGIN_SIDE) -> str:
    """Build one ASS Style line with sane outline/shadow defaults."""
    if border == 3:
        outline_w, shadow = 2, 0
    else:
        outline_w, shadow = 4, 2
    alignment = 8 if "Hook" in name else 2
    return (
        f"Style: {name},{font},{size},{primary},{secondary},"
        f"&H00000000,{box},{bold},{italic},0,0,100,100,0,0,"
        f"{border},{outline_w},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1"
    )


def build_ass_content(
    words_in_segment: list[dict],
    hook_text: str,
    segment_start: float,
    segment_end: float,
    style_cfg: dict | None = None,
    profile: dict = VERTICAL_PROFILE,
) -> tuple[str, dict]:
    """Generate ASS subtitle content with *random* dynamic styling.

    *profile* (VERTICAL_PROFILE or HORIZONTAL_PROFILE) determines the
    canvas size and safe-zone margins/font bounds the hook and karaoke
    captions are fit into — same karaoke-style captioning logic serves
    both the vertical reels and horizontal mid-length clips.

    Returns (ass_content_string, style_info_dict) where style_info
    describes the chosen fonts / colours for logging.
    """
    if style_cfg is None:
        style_cfg = random_style_config()

    hook_s = style_cfg["hook"]
    kara_s = style_cfg["karaoke"]

    # Hook: auto-fit line split + font size to fill the upper safe zone.
    hook_display, hook_font_size = _fit_hook_text(hook_text, profile)

    hook_bold = 1 if hook_s["bold"] else 0
    hook_italic = 1 if hook_s["italic"] else 0
    kara_bold = 1 if kara_s["bold"] else 0
    kara_italic = 1 if kara_s["italic"] else 0

    # Karaoke: build short word-burst chunks first so font size
    # calculation and event generation share the same grouping.
    karaoke_chunks = _build_karaoke_chunks(words_in_segment) if words_in_segment else []

    if karaoke_chunks:
        longest_chunk = max(karaoke_chunks, key=lambda c: sum(len(w["word"]) + 1 for w in c))
        chunk_text = " ".join(w["word"] for w in longest_chunk)
        karaoke_font_size = _fit_karaoke_font(chunk_text, profile)
    else:
        karaoke_font_size = (profile["karaoke_min_font"] + profile["karaoke_max_font"]) // 2

    header = textwrap.dedent(f"""\
    [Script Info]
    ScriptType: v4.00+
    PlayResX: {profile["width"]}
    PlayResY: {profile["height"]}
    ScaledBorderAndShadow: yes

    [V4+ Styles]
    Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
    """)

    hook_style = _style_line(
        "Hook", hook_s["font"], hook_font_size,
        hook_s["color"], hook_s["color"], hook_s["box"],
        hook_bold, hook_italic, hook_s["border_style"],
        margin_v=profile["safe_top"], margin_l=profile["safe_side"], margin_r=profile["safe_side"],
    )
    kara_style = _style_line(
        "Karaoke", kara_s["font"], karaoke_font_size,
        kara_s["primary"], kara_s["secondary"], kara_s["box"],
        kara_bold, kara_italic, kara_s["border_style"],
        margin_v=profile["safe_bottom"], margin_l=profile["safe_side"], margin_r=profile["safe_side"],
    )

    lines = [header, hook_style, kara_style, "",
             "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]

    # Hook event — first 15 seconds (top box)
    rel_duration = segment_end - segment_start
    hook_end_rel = min(HOOK_DURATION, rel_duration)
    hook_text_esc = hook_display.replace("{", "\\{").replace("}", "\\}")
    lines.append(
        f"Dialogue: 0,{fmt_time(0.0)},{fmt_time(hook_end_rel)},Hook,,0,0,0,,{hook_text_esc}"
    )

    # Karaoke — one dialogue event per chunk, each with \k highlighting
    for cw in karaoke_chunks:
        chunk_start_rel = max(0.0, cw[0]["start"] - segment_start)
        chunk_end_rel = max(chunk_start_rel + 0.1, cw[-1]["end"] - segment_start)

        parts = []
        for j, w in enumerate(cw):
            if j + 1 < len(cw):
                cs = max(1, int(round((cw[j + 1]["start"] - w["start"]) * 100)))
            else:
                cs = max(1, int(round((w["end"] - w["start"]) * 100)))
            escaped_word = w["word"].replace("{", "\\{").replace("}", "\\}")
            parts.append(f"{{\\k{cs}}}{escaped_word}")

        karaoke_text = " ".join(parts)
        lines.append(
            f"Dialogue: 0,{fmt_time(chunk_start_rel)},{fmt_time(chunk_end_rel)},Karaoke,,0,0,0,,{karaoke_text}"
        )

    style_info = {
        "hook_font": hook_s["font"],
        "hook_color": hook_s["color"],
        "hook_box": hook_s["box"],
        "hook_bold": hook_s["bold"],
        "hook_italic": hook_s["italic"],
        "karaoke_font": kara_s["font"],
        "karaoke_primary": kara_s["primary"],
        "karaoke_secondary": kara_s["secondary"],
        "karaoke_box": kara_s["box"],
        "karaoke_bold": kara_s["bold"],
        "karaoke_italic": kara_s["italic"],
    }

    return "\n".join(lines), style_info


# ──────────────────────────────────────────────
# STAGE 5 — FFMPEG CLIPPING
# ──────────────────────────────────────────────


def extract_clip(
    source: Path, start: float, duration: float, output: Path
) -> None:
    """Trim a segment from the source video."""
    print(f"[ffmpeg] Extracting clip: {start:.2f}s → {start + duration:.2f}s ...")
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        str(output),
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[ffmpeg] Clip saved to {output}")


def _norm_word(word: str) -> str:
    """Strip punctuation and case for filler-word matching."""
    return re.sub(r"[^\w']", "", word).lower()


def detect_filler_spans(
    words: list[dict], aggressive: bool = False
) -> list[tuple[float, float]]:
    """Find (start, end) time spans of filler words/phrases within *words*.

    Always catches unambiguous interjections (um, uh, erm...). With
    *aggressive*, also catches "like" used standalone and phrases like
    "you know" / "I mean" — riskier since these can be meaningful.

    Each span is padded outward by FILLER_PAD_BEFORE/AFTER to catch the
    acoustic murmur ASR word timestamps tend to clip (particularly the
    trailing edge of a nasal "um"/"hmm"), but never past a neighboring
    word's own timestamp — padding only eats into the natural silence gap
    around the filler, never into real speech. Adjacent/overlapping spans
    are merged.
    """
    spans: list[tuple[float, float]] = []
    n = len(words)
    i = 0
    while i < n:
        matched_len = 0
        if aggressive:
            for phrase in AGGRESSIVE_FILLER_PHRASES:
                plen = len(phrase)
                if i + plen <= n:
                    seq = tuple(_norm_word(words[i + k]["word"]) for k in range(plen))
                    if seq == phrase:
                        matched_len = plen
                        break
        if not matched_len:
            norm = _norm_word(words[i]["word"])
            if norm in FILLER_INTERJECTIONS or (aggressive and norm in AGGRESSIVE_FILLER_WORDS):
                matched_len = 1

        if matched_len:
            raw_start = words[i]["start"]
            raw_end = words[i + matched_len - 1]["end"]
            prev_end = words[i - 1]["end"] if i > 0 else raw_start - FILLER_PAD_BEFORE
            next_start = words[i + matched_len]["start"] if i + matched_len < n else raw_end + FILLER_PAD_AFTER
            padded_start = max(raw_start - FILLER_PAD_BEFORE, prev_end)
            padded_end = min(raw_end + FILLER_PAD_AFTER, next_start)
            spans.append((padded_start, padded_end))
            i += matched_len
        else:
            i += 1

    spans.sort()
    merged: list[tuple[float, float]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def compress_words_timeline(
    words: list[dict],
    seg_start: float,
    seg_end: float,
    filler_spans: list[tuple[float, float]],
) -> tuple[list[dict], float]:
    """Drop filler-span words and remap remaining word timestamps onto a
    new, gap-free timeline starting at 0 (matching the cut clip produced
    by extract_clip_with_cuts).

    Returns (clean_words, new_duration).
    """
    def removed_before(t: float) -> float:
        total = 0.0
        for rs, re in filler_spans:
            if re <= t:
                total += re - rs
            elif rs < t:
                total += t - rs
            else:
                break
        return total

    clean = []
    for w in words:
        if any(rs <= w["start"] < re for rs, re in filler_spans):
            continue
        new_start = (w["start"] - seg_start) - removed_before(w["start"])
        new_end = (w["end"] - seg_start) - removed_before(w["end"])
        clean.append({
            "word": w["word"],
            "start": max(0.0, new_start),
            "end": max(new_start + 0.05, new_end),
        })

    total_removed = sum(re - rs for rs, re in filler_spans)
    new_duration = (seg_end - seg_start) - total_removed
    return clean, new_duration


def extract_clip_with_cuts(
    source: Path,
    seg_start: float,
    seg_end: float,
    filler_spans: list[tuple[float, float]],
    output: Path,
) -> None:
    """Extract a segment from source, splicing out filler-word spans so
    they vanish from both audio and video (not just silenced)."""
    if not filler_spans:
        extract_clip(source, seg_start, seg_end - seg_start, output)
        return

    # Keep ranges, expressed relative to seg_start (matches the -ss seek
    # below, which resets the decoded stream's timestamps to ~0).
    keep_ranges: list[tuple[float, float]] = []
    cursor = seg_start
    for rs, re in filler_spans:
        rs = max(rs, seg_start)
        re = min(re, seg_end)
        if rs > cursor:
            keep_ranges.append((cursor - seg_start, rs - seg_start))
        cursor = max(cursor, re)
    if cursor < seg_end:
        keep_ranges.append((cursor - seg_start, seg_end - seg_start))
    keep_ranges = [(s, e) for s, e in keep_ranges if e - s > 0.02]

    if not keep_ranges:
        extract_clip(source, seg_start, seg_end - seg_start, output)
        return

    print(f"[ffmpeg] Extracting clip with {len(filler_spans)} filler cut(s): "
          f"{seg_start:.2f}s → {seg_end:.2f}s ({len(keep_ranges)} kept piece(s)) ...")

    filter_parts = []
    v_labels, a_labels = [], []
    for i, (s, e) in enumerate(keep_ranges):
        # Short fade in/out on every piece's audio so hard splice points
        # (both at filler cuts and the piece boundaries) never click/pop.
        fade_dur = min(FILLER_CUT_FADE, (e - s) / 2)
        filter_parts.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]"
        )
        filter_parts.append(
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade_dur:.3f},"
            f"afade=t=out:st={(e - s - fade_dur):.3f}:d={fade_dur:.3f}[a{i}]"
        )
        v_labels.append(f"[v{i}]")
        a_labels.append(f"[a{i}]")
    concat_inputs = "".join(f"{v}{a}" for v, a in zip(v_labels, a_labels))
    filter_parts.append(f"{concat_inputs}concat=n={len(keep_ranges)}:v=1:a=1[outv][outa]")
    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seg_start:.3f}",
        "-i", str(source),
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
        str(output),
    ]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"[ffmpeg] Clip saved to {output}")


# ──────────────────────────────────────────────
# STAGE 6 — REFRAMING + SUBTITLES (vertical reels & horizontal mid-length)
# ──────────────────────────────────────────────


def render_reframed(
    clip_path: Path,
    ass_path: Path,
    output_path: Path,
    target_width: int = TARGET_WIDTH,
    target_height: int = TARGET_HEIGHT,
    video_fps: int = TARGET_FPS,
    debug: bool = False,
) -> None:
    """Render clip_path onto a target_width x target_height canvas with a
    blurred/scaled background filling any letterbox/pillarbox gap, plus
    burned-in subtitles. Used for both the vertical 9:16 reels and the
    horizontal 16:9 mid-length clips — for a source already matching the
    target aspect ratio, the blurred background is fully hidden and this
    is just a scale + subtitle burn."""
    log = logger.debug if debug else (lambda *_, **__: None)
    print(f"[ffmpeg] Rendering {target_width}x{target_height} video with blurred background ...")

    fonts_dir = ""
    for candidate in ("/System/Library/Fonts", "/usr/share/fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts")):
        if Path(candidate).exists():
            fonts_dir = candidate
            break
    log("fonts_dir: %s", fonts_dir or "none found — will use system default")

    ass_path_esc = str(ass_path)
    # Escape filter-special characters in the path
    for ch in ":,;=":
        ass_path_esc = ass_path_esc.replace(ch, f"\\{ch}")
    font_opt = f":fontsdir={fonts_dir}" if fonts_dir else ""

    # Fix: do NOT pad v2 — we overlay the un-padded scaled video
    # directly on the blurred background at the centre position.
    # This lets the blurred bg show through the letterbox areas.
    # NOTE: plain scale preserves the source's DAR (16:9) via anamorphic
    # SAR, which would make a 1080x1920 reel display as 16:9. setsar=1
    # forces square pixels so the target aspect ratio is actually rendered.
    filter_complex = (
        f"[0:v]split=2[v1][v2];"
        f"[v1]scale={target_width}:{target_height},setsar=1,boxblur=lr={BLUR_RADIUS}:lp={BLUR_PASSES}[bg];"
        f"[v2]scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"subtitles={ass_path_esc}{font_opt}[outv]"
    )

    log("filter_complex: %s", filter_complex)
    log("ass file: %s", ass_path)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(clip_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-r", str(video_fps),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]
    log("ffmpeg cmd: %s", " ".join(str(a) for a in cmd))

    stderr_target = None if debug else subprocess.DEVNULL
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=stderr_target)
    except subprocess.CalledProcessError as exc:
        if debug:
            print(f"[ffmpeg] ERROR: {exc.stderr.decode() if exc.stderr else 'see above'}")
        raise
    print(f"[ffmpeg] Final output: {output_path}")


# ──────────────────────────────────────────────
# PIPELINE ORCHESTRATOR
# ──────────────────────────────────────────────


def filter_segment_words(
    words: list[dict], seg_start: float, seg_end: float
) -> list[dict]:
    """Return words that overlap with the given time window (with padding)."""
    return [
        w for w in words
        if w["start"] < seg_end and w["end"] > seg_start
    ]


def process_segment(
    segment: dict,
    words: list[dict],
    source_path: Path,
    video_info: dict,
    output_dir: Path,
    index: int,
    debug: bool = False,
    trim_fillers: bool = True,
    aggressive_filler_trim: bool = False,
    profile: dict = VERTICAL_PROFILE,
    target_width: int = TARGET_WIDTH,
    target_height: int = TARGET_HEIGHT,
    filename_prefix: str = "",
) -> Path:
    """Process a single detected segment into a final clip.

    *profile*/*target_width*/*target_height* select vertical-reel vs
    horizontal mid-length rendering; *filename_prefix* keeps the two
    output sets visually distinct in the output directory.
    """
    seg_start = segment["start_time"]
    seg_end = segment["end_time"]
    duration = seg_end - seg_start
    seg_words = filter_segment_words(words, seg_start, seg_end)

    dur_label = f"{duration / 60:.1f}min" if duration >= 120 else f"{duration:.1f}s"
    print(f"\n{'='*60}")
    print(f"Processing {'long-form ' if filename_prefix else ''}clip {index}: {dur_label}")
    print(f"{'='*60}")

    # Filler-word detection — spans are cut from the clip and the words
    # excluded from the karaoke captions; remaining word timestamps are
    # remapped onto the resulting gap-free timeline.
    filler_spans = detect_filler_spans(seg_words, aggressive=aggressive_filler_trim) if trim_fillers else []
    if filler_spans:
        removed = sum(e - s for s, e in filler_spans)
        print(f"    Fillers:  cutting {len(filler_spans)} span(s), {removed:.1f}s removed")
    clean_words, new_duration = compress_words_timeline(seg_words, seg_start, seg_end, filler_spans)
    clean_text = " ".join(w["word"] for w in clean_words) or segment["text"]

    # Generate hook text (from the filler-free transcript for this clip)
    hook = generate_hook(clean_text)
    print(f"    Hook: \"{hook}\"")

    # Generate YouTube + social metadata
    meta = generate_metadata(clean_text, hook)
    print(f"    YouTube: {meta['youtube_title']}")

    # Random styling for this segment
    style_cfg = random_style_config()
    s = style_cfg
    print(f"    Hook font:     {s['hook']['font']}  "
          f"{'B' if s['hook']['bold'] else ''}{'I' if s['hook']['italic'] else ''}")
    print(f"    Karaoke font:  {s['karaoke']['font']}  "
          f"{'B' if s['karaoke']['bold'] else ''}{'I' if s['karaoke']['italic'] else ''}")

    # Create output filename
    slug = slugify(hook) or f"clip_{index:02d}"
    clip_output = output_dir / f"{filename_prefix}{index:02d}_{slug}.mp4"

    # Build ASS subtitles — words are already remapped onto the post-cut,
    # zero-based timeline, so segment_start=0 / segment_end=new_duration.
    ass_content, style_info = build_ass_content(clean_words, hook, 0.0, new_duration, style_cfg, profile=profile)
    if debug:
        logger.debug("ASS content:\n%s", ass_content)
        logger.debug("Style info: %s", style_info)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ass", delete=False, dir=output_dir
    ) as f:
        f.write(ass_content)
        ass_path = Path(f.name)

    try:
        # Extract the raw clip from the source (with filler spans spliced out)
        with tempfile.NamedTemporaryFile(
            suffix=".mp4", delete=False, dir=output_dir
        ) as tmp:
            clip_temp = Path(tmp.name)

        try:
            extract_clip_with_cuts(source_path, seg_start, seg_end, filler_spans, clip_temp)

            # Render reframed video with subtitles
            render_reframed(
                clip_temp,
                ass_path,
                clip_output,
                target_width=target_width,
                target_height=target_height,
                video_fps=video_info["fps"],
                debug=debug,
            )
        finally:
            clip_temp.unlink(missing_ok=True)
    finally:
        ass_path.unlink(missing_ok=True)

    # Save sidecar metadata files
    yt_path = clip_output.with_suffix(".youtube.txt")
    yt_content = (
        f"{meta['youtube_title']}\n\n"
        f"{meta['youtube_description']}\n\n"
        f"{', '.join(meta['youtube_tags'])}\n"
    )
    yt_path.write_text(yt_content, encoding="utf-8")
    print(f"    YouTube metadata: {yt_path.name}")

    social_path = clip_output.with_suffix(".social.txt")
    social_path.write_text(meta["social_caption"] + "\n", encoding="utf-8")
    print(f"    Social caption:   {social_path.name}")

    # RFLXN-branded TikTok caption (music CTA + Palmdale location + hashtags)
    tiktok_path = clip_output.with_suffix(".tiktok.txt")
    tiktok_path.write_text(build_tiktok_caption(hook, clean_text) + "\n", encoding="utf-8")
    print(f"    TikTok caption:   {tiktok_path.name}")

    return clip_output


# ──────────────────────────────────────────────
# STAGE 7 — TIKTOK POSTING (Content Posting API v2 / Direct Post)
# ──────────────────────────────────────────────

TIKTOK_OAUTH_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_VIDEO_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
TIKTOK_CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"


def build_tiktok_caption(hook: str, clip_text: str) -> str:
    """Build the branded, engaging caption used for every TikTok post.

    Determinstic rather than AI-generated so the RFLXN music call-to-action,
    the Palmdale location, and the hashtags are always present and consistent.
    """
    opener = (hook or clip_text or clip_text).strip() or "Check this out"
    lines = [
        opener,
        "",
        f"🚨 {ARTIST_NAME} is back with new sounds 🚨",
        GENRE_LINE,
        STREAMING_LINE,
        MUSIC_TIKTOK_LINE,
        "",
        f"📍 {LOCATION}",
        "",
        " ".join(TIKTOK_HASHTAGS),
    ]
    return "\n".join(lines)


def _credentials_path() -> Path:
    return CREDENTIALS_FILE


def load_credentials() -> dict | None:
    p = _credentials_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_credentials(creds: dict) -> None:
    p = _credentials_path()
    p.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    p.chmod(0o600)


def _request_oauth(payload: dict) -> dict:
    resp = requests.post(TIKTOK_OAUTH_TOKEN_URL, data=payload, timeout=30)
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"TikTok OAuth failed: {data}")
    return data


def _capture_local_callback_code(port: int, expected_state: str) -> str | None:
    """Briefly run a local HTTP server to catch the OAuth redirect.

    Returns the `code` from the callback, or None if the callback never
    arrives or the state parameter doesn't match.
    """
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

    Requires a TikTok Developer app with the `video.publish` scope. The
    client key/secret come from env vars or are prompted for; a local
    loopback callback server captures the OAuth redirect automatically.
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

    code = None
    if redirect_uri.startswith(("http://localhost:", "http://127.0.0.1:")):
        # We can receive the redirect locally — try the loopback server.
        if "TIKTOK_REDIRECT_URI" in os.environ:
            port = int(redirect_uri.rsplit(":", 1)[1])
            code = _capture_local_callback_code(port, state)
            if code is None:
                print("[error] No local callback received. Ensure the redirect URI is "
                      "registered in the TikTok developer portal and re-run.")
    if code is None and not redirect_uri.startswith(("http://localhost:", "http://127.0.0.1:")):
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
    save_credentials(creds)
    print("[tiktok] OAuth complete — credentials saved.")
    return creds


def get_access_token() -> dict:
    """Return fresh credentials, refreshing the access token when it is
    close to expiring. Prompts for a first-time authorisation."""
    creds = load_credentials()
    if creds is None:
        return authorize_tiktok()
    if creds.get("expires_at", 0) <= time.time() + 60:
        if not creds.get("refresh_token"):
            raise RuntimeError("Access token expired and no refresh token exists — "
                               "re-run `--tiktok-auth`.")
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
        save_credentials(creds)
        print("[tiktok] Access token refreshed.")
    return creds


def _tiktok_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def tiktok_query_creator_info(access_token: str) -> dict:
    """Ask TikTok which privacy levels / durations the connected account
    supports, so the init request can stay within the allowed options."""
    resp = requests.post(
        TIKTOK_CREATOR_INFO_URL,
        headers=_tiktok_headers(access_token),
        json={},
        timeout=30,
    )
    data = resp.json()
    if data.get("error", {}).get("code") != "ok":
        raise RuntimeError(f"creator_info query failed: {data}")
    return data.get("data", {})


def tiktok_init_post(
    access_token: str, video_path: Path, caption: str,
    privacy_level: str = "PUBLIC_TO_EVERYONE",
) -> tuple[str, str | None]:
    """Call POST /v2/post/publish/video/init/ for a FILE_UPLOAD post."""
    size = video_path.stat().st_size
    chunk_size = min(TIKTOK_UPLOAD_CHUNK_SIZE, size)
    total_chunks = max(1, (size + chunk_size - 1) // chunk_size)

    body = {
        "post_info": {
            "title": caption,
            "privacy_level": privacy_level,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }
    resp = requests.post(
        TIKTOK_VIDEO_INIT_URL, headers=_tiktok_headers(access_token),
        json=body, timeout=30,
    )
    data = resp.json()
    if data.get("error", {}).get("code") != "ok":
        raise RuntimeError(f"video init failed: {data}")
    publish_id = data["data"]["publish_id"]
    upload_url = data["data"].get("upload_url")
    return publish_id, upload_url


def tiktok_upload_media(
    upload_url: str, video_path: Path,
    chunk_size: int = TIKTOK_UPLOAD_CHUNK_SIZE,
) -> None:
    """PUT the video file to the upload_url using Content-Range chunking."""
    size = video_path.stat().st_size
    with open(video_path, "rb") as fh:
        offset = 0
        while offset < size:
            fh.seek(offset)
            chunk = fh.read(chunk_size)
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


def tiktok_fetch_post_status(access_token: str, publish_id: str) -> tuple[str, str]:
    """POST /v2/post/publish/status/fetch/ → (status, fail_reason)."""
    resp = requests.post(
        TIKTOK_STATUS_URL, headers=_tiktok_headers(access_token),
        json={"publish_id": publish_id}, timeout=30,
    )
    data = resp.json()
    if data.get("error", {}).get("code") != "ok":
        raise RuntimeError(f"status fetch failed: {data}")
    d = data.get("data", {})
    return d.get("status", ""), d.get("fail_reason", "")


def publish_video_to_tiktok(
    video_path: Path, caption: str,
    privacy_level: str = "PUBLIC_TO_EVERYONE",
    wait: bool = True, dry_run: bool = False,
) -> str:
    """Upload + publish one video to TikTok via the Direct Post API.

    Returns the TikTok publish_id (or "DRY_RUN").
    """
    print(f"\n[tiktok] Publishing {video_path.name} ...")
    if dry_run:
        print("[tiktok]   (dry run — nothing uploaded)")
        return "DRY_RUN"

    creds = get_access_token()
    publish_id, upload_url = tiktok_init_post(
        creds["access_token"], video_path, caption, privacy_level
    )
    if upload_url:
        print("[tiktok]   Uploading video ...")
        tiktok_upload_media(upload_url, video_path)

    if not wait:
        print(f"[tiktok]   Initiated, publish_id={publish_id}")
        return publish_id

    deadline = time.time() + TIKTOK_STATUS_TIMEOUT
    status, reason = "PROCESSING_UPLOAD", ""
    while status not in ("PUBLISH_COMPLETE", "FAILED") and time.time() < deadline:
        time.sleep(TIKTOK_STATUS_POLL_SEC)
        status, reason = tiktok_fetch_post_status(creds["access_token"], publish_id)
        print(f"[tiktok]   status: {status}")

    if status == "PUBLISH_COMPLETE":
        print(f"[tiktok] Published ✓ {video_path.name}")
        return publish_id
    raise RuntimeError(f"TikTok publish failed ({status}): {reason}")


def load_schedule() -> list[dict]:
    if not SCHEDULE_FILE.exists():
        return []
    try:
        return json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("[schedule] corrupt queue — starting fresh.")
        return []


def save_schedule(items: list[dict]) -> None:
    SCHEDULE_FILE.write_text(json.dumps(items, indent=2), encoding="utf-8")
    print(f"[schedule] Queue saved: {SCHEDULE_FILE.name} ({len(items)} video(s))")


def load_clip_caption(clip_path: Path) -> str:
    """Read the brand caption written alongside each clip (.tiktok.txt)."""
    sidecar = clip_path.with_suffix(".tiktok.txt")
    if sidecar.exists():
        return sidecar.read_text(encoding="utf-8").strip()
    return ""


def site_url_for(clip_path: Path) -> str:
    """Return the GitHub Pages URL a generated clip maps to once pushed to
    the repo — https://rtmalikian.github.io/itsrflxn-tiktok-api/videos/<name>."""
    return f"{TIKTOK_SITE_BASE}/{TIKTOK_VIDEO_DIR}/{clip_path.name}"


def plan_daily_schedule(
    clip_paths: list[Path],
    start_time: str = DAILY_PUBLISH_TIME,
    interval_days: int = DAILY_INTERVAL_DAYS,
) -> list[dict]:
    """Assign each clip a daily publish slot starting tomorrow at start_time."""
    hh, mm = (int(x) for x in start_time.split(":"))
    now = datetime.now()
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=1)

    items: list[dict] = []
    for i, clip in enumerate(clip_paths):
        publish_at = slot + timedelta(days=interval_days * i)
        if publish_at <= now:
            publish_at = now + timedelta(minutes=1)
        caption = load_clip_caption(clip) or build_tiktok_caption("", clip.stem)
        items.append({
            "video": str(clip.resolve()),
            "caption": caption,
            "url": site_url_for(clip),
            "publish_at": publish_at.isoformat(),
            "published": False,
            "publish_id": None,
        })
    return items


def post_due_scheduled(
    privacy_level: str,
    dry_run: bool = False,
    now: datetime | None = None,
) -> list[dict]:
    """Publish every queued slot whose time has arrived. Returns the items posted."""
    now = now or datetime.now()
    items = load_schedule()
    due = [
        it for it in items
        if not it.get("published") and datetime.fromisoformat(it["publish_at"]) <= now
    ]
    pending = sum(1 for it in items if not it.get("published"))
    if not due:
        print(f"[schedule] Nothing due yet ({pending} queued).")
        return []

    published = []
    for it in due:
        print(f"[schedule] Posting (slot {it['publish_at']}): {Path(it['video']).name}")
        if it.get("url"):
            print(f"[schedule]   URL: {it['url']}")
        try:
            pub_id = publish_video_to_tiktok(
                Path(it["video"]), it["caption"], privacy_level, dry_run=dry_run
            )
        except RuntimeError as e:
            print(f"[error] {e}")
            continue
        it["published"] = True
        it["publish_id"] = pub_id
        published.append(it)

    save_schedule(items)
    return published


def watch_schedule(
    privacy_level: str, dry_run: bool = False, poll_sec: int = 60
) -> None:
    """Keep running until every scheduled video has been published."""
    while True:
        remaining = [it for it in load_schedule() if not it.get("published")]
        if not remaining:
            print("[schedule] All queued videos published.")
            return
        post_due_scheduled(privacy_level, dry_run=dry_run)
        next_due = min(datetime.fromisoformat(it["publish_at"]) for it in remaining)
        print(
            f"[schedule] Next post due {next_due.isoformat()} "
            f"— checking again every {poll_sec}s ..."
        )
        time.sleep(poll_sec)


@click.command()
@click.option(
    "--input", "-i", "input_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Absolute path to the source video (.mp4/.mkv/.mov). Skips the interactive prompt.",
)
@click.option(
    "--whisper-model",
    default=None,
    help="Whisper model size (tiny/base/small/medium/large-v3)",
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    help="Enable verbose FFmpeg and diagnostic output",
)
@click.option(
    "--force-transcribe",
    is_flag=True,
    default=False,
    help="Ignore cached transcript and re-transcribe from scratch",
)
@click.option(
    "--trim-fillers/--no-trim-fillers",
    default=True,
    help="Cut filler interjections (um, uh, erm...) out of each clip",
)
@click.option(
    "--aggressive-filler-trim",
    is_flag=True,
    default=False,
    help="Also cut standalone 'like', 'you know', 'I mean', etc. "
         "(riskier — these can be meaningful, not just verbal tics)",
)
@click.option(
    "--longform/--no-longform",
    default=True,
    help="Also scan for and render mid-length 16:9 clips (monologues / "
         "motte-and-bailey arcs) for YouTube & Instagram, alongside the reels",
)
@click.option(
    "--upload/--no-upload",
    "upload",
    default=False,
    help="Post generated clips to TikTok via the Content Posting API "
         "(run --tiktok-auth once first to grant video.publish)",
)
@click.option(
    "--mode",
    "tiktok_mode",
    type=click.Choice(["schedule", "immediate"]),
    default="schedule",
    help="One clip per day (schedule) or upload and publish everything now (immediate)",
)
@click.option(
    "--time",
    "publish_time",
    default=DAILY_PUBLISH_TIME,
    show_default=True,
    help="Local HH:MM at which the daily scheduled post goes out",
)
@click.option(
    "--interval-days",
    "interval_days",
    type=int,
    default=DAILY_INTERVAL_DAYS,
    show_default=True,
    help="Days between each scheduled TikTok post",
)
@click.option(
    "--privacy",
    "privacy_level",
    type=click.Choice(["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"]),
    default="PUBLIC_TO_EVERYONE",
    show_default=True,
    help="TikTok post privacy level (must be allowed by the target account)",
)
@click.option(
    "--watch",
    is_flag=True,
    default=False,
    help="Keep running until every scheduled TikTok post has gone out",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print/schedule TikTok actions without actually publishing",
)
@click.option(
    "--tiktok-auth",
    is_flag=True,
    default=False,
    help="Run the TikTok OAuth authorisation flow and exit",
)
def main(
    input_path: Path | None,
    whisper_model: str | None,
    debug: bool,
    force_transcribe: bool,
    trim_fillers: bool,
    aggressive_filler_trim: bool,
    longform: bool,
    upload: bool,
    tiktok_mode: str,
    publish_time: str,
    interval_days: int,
    privacy_level: str,
    watch: bool,
    dry_run: bool,
    tiktok_auth: bool,
):
    """Gen — Transform horizontal videos into viral 9:16 shorts (plus mid-length 16:9 clips)."""
    global WHISPER_MODEL_SIZE

    if debug:
        logger.setLevel(_logging.DEBUG)
        logger.debug("Debug mode enabled")

    # ── TikTok OAuth bootstrap ──
    if tiktok_auth:
        authorize_tiktok()
        print("[tiktok] Authorisation complete — you can now run the normal flow.")
        return

    print("\n  Gen — Vertical Short-Form Video Generator (RFLXN edition)\n")

    # Verify FFmpeg has libass (subtitles filter)
    _check_ffmpeg_libass()

    # Probe available fonts
    hook_fonts, karaoke_fonts = available_fonts()
    print(f"[fonts] Hook pool:    {len(hook_fonts)} fonts — {', '.join(hook_fonts)}")
    print(f"[fonts] Karaoke pool: {len(karaoke_fonts)} fonts — {', '.join(karaoke_fonts)}")
    print()

    # Model picker — interactive if not passed via CLI
    if whisper_model:
        WHISPER_MODEL_SIZE = whisper_model
    else:
        model_choices = [
            ("tiny     (fastest, lowest accuracy)",        "tiny"),
            ("base     (fast, okay accuracy)",             "base"),
            ("small    (balanced)",                        "small"),
            ("medium   (handles music / noise well)",      "medium"),
            ("large-v3 (slowest, best accuracy)",          "large-v3"),
        ]
        default_idx = next(i for i, (_, v) in enumerate(model_choices) if v == WHISPER_MODEL_SIZE)
        raw = questionary.select(
            "Select Whisper transcription model:",
            choices=[c[0] for c in model_choices],
            default=model_choices[default_idx][0],
        ).ask()
        if raw is None:
            print("[abort] User cancelled.")
            sys.exit(0)
        WHISPER_MODEL_SIZE = next(v for lbl, v in model_choices if lbl == raw)
    print(f"[whisper] Model: {WHISPER_MODEL_SIZE}\n")

    # ── Stage 1: File selection ──
    source = pick_video(input_path)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n[info] Source: {source}")
    print(f"[info] Output: {OUTPUT_DIR}/\n")

    # ── Video info ──
    info = get_video_info(source)
    print(f"[info] Duration: {info['duration']:.1f}s | "
          f"Resolution: {info['width']}x{info['height']} | "
          f"FPS: {info['fps']}")

    # ── Stage 2: Transcription (with optional cache) ──
    words: list[dict] | None = None
    if not force_transcribe:
        words = load_transcript_cache(source)
    if words is None:
        words = transcribe(source)
        save_transcript_cache(words, source)

    # ── Stage 3: AI segment detection (reels) ──
    segments = detect_viral_segments(words, info["duration"])

    # ── Stage 4-6: Process each reel segment ──
    clip_outputs = []
    for i, seg in enumerate(segments, 1):
        out = process_segment(
            seg, words, source, info, OUTPUT_DIR, i, debug=debug,
            trim_fillers=trim_fillers, aggressive_filler_trim=aggressive_filler_trim,
        )
        clip_outputs.append(out)

    # ── Stage 3b/4-6: Mid-length 16:9 clips (monologues / motte-and-bailey) ──
    if longform:
        longform_segments = detect_longform_segments(words, info["duration"])
        for i, seg in enumerate(longform_segments, 1):
            out = process_segment(
                seg, words, source, info, OUTPUT_DIR, i, debug=debug,
                trim_fillers=trim_fillers, aggressive_filler_trim=aggressive_filler_trim,
                profile=HORIZONTAL_PROFILE,
                target_width=TARGET_WIDTH_H, target_height=TARGET_HEIGHT_H,
                filename_prefix="long_",
            )
            clip_outputs.append(out)

    # ── Done ──
    print(f"\n{'='*60}")
    print("All clips generated successfully!")
    print(f"{'='*60}")
    for p in clip_outputs:
        file_size = p.stat().st_size / (1024 * 1024)
        print(f"  {p.name}  ({file_size:.1f} MB)")
        print(f"    → {site_url_for(p)}")

    # ── Stage 7: TikTok posting (Content Posting API v2 / Direct Post) ──
    if upload and clip_outputs:
        if tiktok_mode == "immediate":
            for p in clip_outputs:
                caption = load_clip_caption(p) or build_tiktok_caption("", p.stem)
                publish_video_to_tiktok(
                    p, caption, privacy_level, dry_run=dry_run
                )
        else:
            # Schedule one video per day, starting tomorrow at publish_time.
            items = load_schedule()
            queued = {Path(it["video"]).resolve() for it in items if not it.get("published")}
            new_clips = [p for p in clip_outputs if Path(p).resolve() not in queued]
            if new_clips:
                items.extend(plan_daily_schedule(new_clips, publish_time, interval_days))
                save_schedule(items)
            else:
                print(f"[schedule] All {len(clip_outputs)} clip(s) already in the queue.")
            post_due_scheduled(privacy_level, dry_run=dry_run)
            if watch:
                watch_schedule(privacy_level, dry_run=dry_run)


if __name__ == "__main__":
    main()
