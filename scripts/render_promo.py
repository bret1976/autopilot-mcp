#!/usr/bin/env python3
"""Assemble a 60s letterboxed explainer from stills + kinetic captions."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
ASSETS = Path("/opt/cursor/artifacts/assets")
WORK = ROOT / "tmp" / "promo"
W, H = 1920, 1080
# 2.39:1 window inside 16:9
BOX = 804
BAR = (H - BOX) // 2

SERIF = "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf"
SANS = "/usr/share/fonts/truetype/macos/Inter-Regular.ttf"
MONO = "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Regular.ttf"

BEATS = [
    (ASSETS / "frame-01-projector.png", 6.5, "6FRAME AUTOPILOT", "A locked spine. Not a dashboard."),
    (ASSETS / "frame-02-dashboards.png", 7.0, "PLATFORMS WANT THEIR UI", "You already pay for a model."),
    (ASSETS / "frame-03-one-link.png", 7.0, "ONE MCP LINK", "Private. Signed. Yours."),
    (ASSETS / "frame-04-paste.png", 7.0, "PASTE IT INTO CLAUDE", "Or Cursor. Or Codex."),
    (ASSETS / "frame-05-keys.png", 7.0, "YOUR KEYS", "Gemini + PostProxy. Never ours."),
    (ASSETS / "frame-06-spine.png", 8.5, "SCAN  ·  CUT  ·  WRITE  ·  POST", "The original. Under 60 seconds."),
    (ASSETS / "frame-07-formats.png", 8.0, "9:16 SHORTS   16:9 DESKS", "IG / TikTok / YT / FB   ·   LinkedIn / X"),
    (ASSETS / "frame-08-keep.png", 5.5, "KEEP THE MODEL YOU PAY FOR", "Work lives where you already work."),
    (ASSETS / "frame-09-end.png", 3.5, "$997 ONCE", "6Frame Studio"),
]


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr or proc.stdout or "ffmpeg failed")


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, (src, dur, title, sub) in enumerate(BEATS):
        if not src.exists():
            raise SystemExit(f"missing still: {src}")
        out = WORK / f"beat-{i:02d}.mp4"
        font = SERIF if Path(SERIF).exists() else SANS
        draw = (
            f"drawbox=x=0:y=0:w=iw:h={BAR}:color=black@1:t=fill,"
            f"drawbox=x=0:y=ih-{BAR}:w=iw:h={BAR}:color=black@1:t=fill,"
            f"drawtext=fontfile={font}:text='{title}':fontcolor=0xDCDAD6:fontsize=56:"
            f"x=(w-text_w)/2:y=h-{BAR}-120:shadowcolor=black@0.7:shadowx=2:shadowy=2,"
            f"drawtext=fontfile={SANS}:text='{sub}':fontcolor=0xB8B6B1:fontsize=24:"
            f"x=(w-text_w)/2:y=h-{BAR}-58"
        )
        run(
            [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-i",
                str(src),
                "-t",
                str(dur),
                "-vf",
                f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},zoompan=z='min(zoom+0.0008,1.08)':d=1:s={W}x{H},{draw}",
                "-r",
                "24",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(out),
            ]
        )
        clips.append(out)

    concat = WORK / "list.txt"
    concat.write_text("".join(f"file '{p}'\n" for p in clips), encoding="utf-8")
    promo = PUBLIC / "promo.mp4"
    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-vf",
            "fade=t=in:st=0:d=0.6,fade=t=out:st=59.2:d=0.7",
            "-t",
            "60",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(promo),
        ]
    )
    print(promo, promo.stat().st_size)


if __name__ == "__main__":
    main()
