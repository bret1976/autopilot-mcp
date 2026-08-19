#!/usr/bin/env python3
"""Assemble a 75s letterboxed explainer — this product only."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
ASSETS = Path("/opt/cursor/artifacts/assets")
WORK = ROOT / "tmp" / "promo"
W, H = 1920, 1080
BOX = 804
BAR = (H - BOX) // 2

SERIF = "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf"
SANS = "/usr/share/fonts/truetype/macos/Inter-Regular.ttf"

# 7 beats from the brief. Total 75s (inside 45–90).
BEATS = [
    (ASSETS / "frame-04-paste.png", 11.0, "YOUR BUYER ALREADY LIVES IN CLAUDE", "Stop making them live in your app."),
    (ASSETS / "frame-02-dashboards.png", 10.0, "TIME. SKILL. PRETTY UIS.", "Free now. That moat is dead."),
    (ASSETS / "frame-06-spine.png", 12.0, "THEY BUY ACCESS", "A workflow that already knows Autopilot."),
    (ASSETS / "frame-03-one-link.png", 14.0, "PASTE ONE MCP LINK", "Run Autopilot. Scan. Trim. Hashtags. Post."),
    (ASSETS / "frame-08-keep.png", 9.0, "STUDIOS AND OPERATORS", "Tired of onboarding people onto a platform."),
    (ASSETS / "frame-05-keys.png", 9.0, "THEY CAN DIY. THEY WON'T.", "If the workflow is one paste away."),
    (ASSETS / "frame-09-end.png", 10.0, "GET THE MCP LINK", "$297 once. Not a trial."),
]


def escape_draw(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")
        .replace("%", "\\%")
    )


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr or proc.stdout or "ffmpeg failed")


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)
    clips = []
    font = SERIF if Path(SERIF).exists() else SANS
    for i, (src, dur, title, sub) in enumerate(BEATS):
        if not src.exists():
            raise SystemExit(f"missing still: {src}")
        out = WORK / f"beat-{i:02d}.mp4"
        draw = (
            f"drawbox=x=0:y=0:w=iw:h={BAR}:color=black@1:t=fill,"
            f"drawbox=x=0:y=ih-{BAR}:w=iw:h={BAR}:color=black@1:t=fill,"
            f"drawtext=fontfile={font}:text='{escape_draw(title)}':fontcolor=0xDCDAD6:fontsize=44:"
            f"x=(w-text_w)/2:y=h-{BAR}-118:shadowcolor=black@0.7:shadowx=2:shadowy=2,"
            f"drawtext=fontfile={SANS}:text='{escape_draw(sub)}':fontcolor=0xB8B6B1:fontsize=22:"
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
                f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},{draw}",
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
            "fade=t=in:st=0:d=0.5,fade=t=out:st=74.3:d=0.6",
            "-t",
            "75",
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
