"""Spoken walkthrough locked to each hero beat.

Uses a casual neural read, then a light room/compressor so it sits
on the picture like a person talking over their shoulder — not a
spotless announcer track.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import edge_tts

VOICE = "en-US-BrianNeural"
RATE = "-6%"
PITCH = "-2Hz"

# One spoken thought per picture beat. Contractions, short sentences,
# commas for breath. Do not write like a brochure.
LINES: dict[str, str] = {
    "open": (
        "Your buyer already lives in Claude. "
        "Stop making them live in some other app."
    ),
    "cockpit": (
        "This is Autopilot — the real cockpit. "
        "Six platforms. Hit Run Pipeline, and that queue actually posts."
    ),
    "spine": (
        "That's the job. Scan a viral original. Grab it. Write the copy. Post it."
    ),
    "scan": (
        "Here's the scan. Viral cuts from today. Those success rows are live jobs."
    ),
    "link": (
        "They don't learn this desk. One link, pasted into Claude. That's the install."
    ),
    "proxy": (
        "PostProxy is already live. Instagram, TikTok, YouTube, LinkedIn, X — their account."
    ),
    "copy": (
        "Gemini writes the replies in their voice. Change a line if you want. Then send it."
    ),
    "why": (
        "Steps stay locked. Brand stays theirs. They never have to live in your software."
    ),
    "workshop": (
        "Same spine in the workshop. Paste the original, scrape it, trim it, ship it."
    ),
    "cta": (
        "You're selling the workflow, not a login. Get the MCP link. Nine ninety-seven, once."
    ),
}


def _duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        text=True,
    ).strip()
    return float(out)


async def _speak(text: str, dest: Path) -> None:
    raw = dest.with_suffix(".raw.mp3")
    communicate = edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH)
    await communicate.save(str(raw))
    # Dry close-mic TTS is what reads as "AI". A little chest, a little room,
    # a hair of pink air — like someone sat a room away from the laptop.
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(raw),
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=pink:amplitude=0.004:sample_rate=44100",
            "-filter_complex",
            (
                "[0:a]highpass=f=80,lowpass=f=9800,"
                "acompressor=threshold=-18dB:ratio=2.2:attack=16:release=120:makeup=6,"
                "aecho=0.88:0.8:16:0.04,"
                "volume=7.5dB,"
                "alimiter=limit=0.96[v];"
                "[1:a]volume=0.015[n];"
                "[v][n]amix=inputs=2:duration=first:dropout_transition=0,"
                "alimiter=limit=0.96[out]"
            ),
            "-map",
            "[out]",
            "-ar",
            "44100",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    raw.unlink(missing_ok=True)


def render_line(key: str, dest: Path) -> float:
    dest.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_speak(LINES[key], dest))
    return _duration(dest)


def pad_to(src: Path, dest: Path, seconds: float) -> None:
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-af",
            f"apad=whole_dur={seconds:.3f}",
            "-t",
            f"{seconds:.3f}",
            "-ar",
            "44100",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
