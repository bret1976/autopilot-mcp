#!/usr/bin/env python3
"""Hybrid hero: value cards interleaved with live Autopilot app footage.

Not title cards only. Not a 97s dashboard tour. Back and forth — what it
is worth, then the cockpit / scan / PostProxy / workshop actually running.
"""

from __future__ import annotations

import subprocess
import tempfile
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "public"
SOURCE = ROOT / "scripts" / "assets" / "live-autopilot.mp4"
SOURCE_GIT = "88e0102:public/promo.mp4"
W, H = 1920, 1080
BG = (12, 15, 13)
GOLD = (196, 165, 116)
INK = (243, 238, 230)
MUTED = (167, 162, 154)
LINE = (38, 44, 40)
CARD = (20, 25, 22)
BG_HEX = "0c0f0d"

FONT_URLS = {
    "serif": "https://github.com/google/fonts/raw/main/ofl/cormorantgaramond/CormorantGaramond%5Bwght%5D.ttf",
    "serif_italic": "https://github.com/google/fonts/raw/main/ofl/cormorantgaramond/CormorantGaramond-Italic%5Bwght%5D.ttf",
    "sans": "https://github.com/google/fonts/raw/main/ofl/manrope/Manrope%5Bwght%5D.ttf",
}


def _font_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "autopilot-explainer-fonts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fetch_fonts() -> dict[str, Path]:
    folder = _font_dir()
    out: dict[str, Path] = {}
    for name, url in FONT_URLS.items():
        dest = folder / f"{name}.ttf"
        if not dest.exists() or dest.stat().st_size < 1000:
            urllib.request.urlretrieve(url, dest)
        out[name] = dest
    return out


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size=size)


def _new() -> Image.Image:
    return Image.new("RGB", (W, H), BG)


def _center(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, y: int, fill=INK) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    x = (W - (box[2] - box[0])) // 2
    draw.text((x, y), text, font=font, fill=fill)


def _wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    y: int,
    fill=MUTED,
    width: int = 1400,
    gap: int = 12,
) -> int:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        box = draw.textbbox((0, 0), trial, font=font)
        if box[2] - box[0] <= width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    for line in lines:
        _center(draw, line, font, y, fill)
        box = draw.textbbox((0, 0), line, font=font)
        y += (box[3] - box[1]) + gap
    return y


def _split_title(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        box = draw.textbbox((0, 0), trial, font=font)
        if box[2] - box[0] <= width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def scene_kicker_title(fonts: dict[str, Path], kicker: str, title: str, sub: str = "") -> Image.Image:
    im = _new()
    draw = ImageDraw.Draw(im)
    serif = _font(fonts["serif"], 86)
    italic = _font(fonts["serif_italic"], 86)
    sans = _font(fonts["sans"], 28)
    small = _font(fonts["sans"], 26)
    _center(draw, kicker.upper(), sans, 280, GOLD)
    if ". " in title:
        first, rest = title.split(". ", 1)
        _center(draw, first + ".", serif, 380, INK)
        _center(draw, rest, italic, 490, GOLD)
    else:
        y = 380
        for line in _split_title(draw, title, serif, 1600):
            _center(draw, line, serif, y, INK)
            y += 100
    if sub:
        _wrapped(draw, sub, small, 680, MUTED, 1200)
    return im


def scene_workflow(fonts: dict[str, Path]) -> Image.Image:
    im = _new()
    draw = ImageDraw.Draw(im)
    sans = _font(fonts["sans"], 24)
    serif = _font(fonts["serif"], 64)
    card_font = _font(fonts["serif"], 36)
    hint = _font(fonts["sans"], 22)
    _center(draw, "THE WORKFLOW YOU BUY", sans, 140, GOLD)
    _center(draw, "Scan. Grab. Write. Post.", serif, 200, INK)
    steps = ("SCAN", "GRAB ORIGINAL", "WRITE COPY", "POST")
    hints = ("viral cut", "trim < 60s", "6Frame voice", "PostProxy × 6")
    box_w, box_h, gap = 360, 220, 36
    total = 4 * box_w + 3 * gap
    x0 = (W - total) // 2
    y0 = 430
    for i, (label, caption) in enumerate(zip(steps, hints, strict=True)):
        x = x0 + i * (box_w + gap)
        draw.rounded_rectangle((x, y0, x + box_w, y0 + box_h), radius=18, fill=CARD, outline=GOLD, width=2)
        _center_in(draw, label, card_font, x, y0 + 58, box_w, GOLD)
        _center_in(draw, caption, hint, x, y0 + 130, box_w, MUTED)
        if i < 3:
            ax = x + box_w + 8
            draw.polygon(
                [(ax, y0 + box_h // 2 - 10), (ax + 18, y0 + box_h // 2), (ax, y0 + box_h // 2 + 10)],
                fill=GOLD,
            )
    _center(draw, "Watch it run on the live Autopilot app — then paste the same spine into Claude.", hint, 760, MUTED)
    return im


def _center_in(draw, text, font, x, y, width, fill) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((x + (width - (box[2] - box[0])) // 2, y), text, font=font, fill=fill)


def scene_steps(fonts: dict[str, Path]) -> Image.Image:
    im = _new()
    draw = ImageDraw.Draw(im)
    sans = _font(fonts["sans"], 24)
    serif = _font(fonts["serif"], 64)
    num = _font(fonts["serif"], 48)
    body = _font(fonts["sans"], 26)
    _center(draw, "ONE LINK", sans, 130, GOLD)
    _center(draw, "Paste. Setup. Run Autopilot.", serif, 190, INK)
    items = (
        ("1", "Paste the URL into Claude", "Settings → Connectors → Add custom connector"),
        ("2", "setup with their keys", "Gemini + PostProxy. Brand is a sentence."),
        ("3", "“Run Autopilot.”", "That same scan → copy → post spine."),
    )
    y = 340
    for n, title, detail in items:
        draw.rounded_rectangle((360, y, 1560, y + 150), radius=16, fill=CARD, outline=LINE, width=1)
        draw.text((400, y + 42), n, font=num, fill=GOLD)
        draw.text((500, y + 36), title, font=serif, fill=INK)
        draw.text((500, y + 96), detail, font=body, fill=MUTED)
        y += 180
    return im


def scene_why(fonts: dict[str, Path]) -> Image.Image:
    im = _new()
    draw = ImageDraw.Draw(im)
    sans = _font(fonts["sans"], 24)
    serif = _font(fonts["serif"], 70)
    body = _font(fonts["sans"], 30)
    _center(draw, "WHY MCP", sans, 220, GOLD)
    _center(draw, "You watched the app. You do not live in it.", serif, 300, INK)
    _wrapped(draw, "Steps stay locked. Language and brand stay theirs.", body, 460, MUTED, 1400)
    _wrapped(draw, "The workflow ships as one private URL.", body, 540, MUTED, 1400)
    return im


def scene_cta(fonts: dict[str, Path]) -> Image.Image:
    im = _new()
    draw = ImageDraw.Draw(im)
    sans = _font(fonts["sans"], 24)
    serif = _font(fonts["serif"], 92)
    italic = _font(fonts["serif_italic"], 48)
    _center(draw, "6FRAME AUTOPILOT", sans, 280, GOLD)
    _center(draw, "Get the MCP link.", serif, 380, INK)
    _center(draw, "$997 once.", italic, 520, GOLD)
    _center(draw, "Same scan. Same PostProxy. Inside the AI they already pay for.", sans, 680, MUTED)
    return im


def lower_third(fonts: dict[str, Path], kicker: str, line: str) -> Image.Image:
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 930, W, H), fill=(12, 15, 13, 235))
    draw.rectangle((0, 928, W, 932), fill=GOLD + (255,))
    sans = _font(fonts["sans"], 20)
    serif = _font(fonts["serif"], 40)
    draw.text((80, 948), kicker.upper(), font=sans, fill=GOLD)
    draw.text((80, 980), line, font=serif, fill=INK)
    return im


def ensure_source() -> Path:
    if SOURCE.exists() and SOURCE.stat().st_size > 100_000:
        return SOURCE
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    blob = subprocess.check_output(["git", "-C", str(ROOT), "show", SOURCE_GIT])
    SOURCE.write_bytes(blob)
    return SOURCE


def _encode_card(png: Path, dest: Path, seconds: float) -> None:
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(png),
            "-t",
            f"{seconds:.2f}",
            "-vf",
            f"fade=t=in:st=0:d=0.25,fade=t=out:st={seconds - 0.25:.2f}:d=0.25,format=yuv420p",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(dest),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _encode_app(src: Path, start: float, seconds: float, overlay: Path, dest: Path) -> None:
    # Crop browser chrome + old burned captions; letterbox on the landing black.
    vf = (
        f"crop=1280:600:0:92,scale=1920:900,"
        f"pad=1920:1080:0:28:color={BG_HEX},"
        f"fade=t=in:st=0:d=0.25,fade=t=out:st={seconds - 0.25:.2f}:d=0.25"
    )
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.2f}",
            "-i",
            str(src),
            "-loop",
            "1",
            "-i",
            str(overlay),
            "-t",
            f"{seconds:.2f}",
            "-filter_complex",
            f"[0:v]{vf}[v];[v][1:v]overlay=0:0,format=yuv420p[out]",
            "-map",
            "[out]",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(dest),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def render() -> None:
    fonts = _fetch_fonts()
    source = ensure_source()
    PUBLIC.mkdir(parents=True, exist_ok=True)

    cards = {
        "open": scene_kicker_title(
            fonts,
            "Cold open",
            "Your buyer already lives in Claude. Stop making them live in your app.",
        ),
        "spine": scene_workflow(fonts),
        "link": scene_steps(fonts),
        "why": scene_why(fonts),
        "cta": scene_cta(fonts),
    }
    thirds = {
        "cockpit": lower_third(fonts, "Live Autopilot", "Cockpit. Six platforms. Run the pipeline."),
        "scan": lower_third(fonts, "Viral scan", "Scan Social Trends. The queue is real."),
        "proxy": lower_third(fonts, "PostProxy", "Instagram, TikTok, YouTube, LinkedIn, X — LIVE."),
        "copy": lower_third(fonts, "Write copy", "Gemini rewrites it in 6Frame voice."),
        "workshop": lower_third(fonts, "Grab the original", "Analyze & scrape. Trim. Ship."),
    }

    # Source timestamps from the live Railway Autopilot recording.
    timeline: list[tuple[str, object]] = [
        ("card", "open", 5.0),
        ("app", "cockpit", 15.2, 8.0),
        ("card", "spine", 5.0),
        ("app", "scan", 6.8, 8.0),
        ("card", "link", 5.5),
        ("app", "proxy", 23.8, 7.5),
        ("app", "copy", 49.5, 8.0),
        ("card", "why", 5.0),
        ("app", "workshop", 36.8, 6.5),
        ("card", "cta", 7.0),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clips: list[Path] = []
        poster_set = False
        for i, beat in enumerate(timeline):
            dest = tmp_path / f"beat-{i:02d}.mp4"
            if beat[0] == "card":
                _, key, seconds = beat
                png = tmp_path / f"card-{key}.png"
                cards[key].save(png, "PNG")
                _encode_card(png, dest, float(seconds))
            else:
                _, key, start, seconds = beat
                overlay = tmp_path / f"third-{key}.png"
                thirds[key].save(overlay, "PNG")
                _encode_app(source, float(start), float(seconds), overlay, dest)
                if not poster_set:
                    subprocess.check_call(
                        [
                            "ffmpeg",
                            "-y",
                            "-ss",
                            f"{float(start) + 1.2:.2f}",
                            "-i",
                            str(source),
                            "-frames:v",
                            "1",
                            "-vf",
                            f"crop=1280:600:0:92,scale=1920:900,pad=1920:1080:0:28:color={BG_HEX}",
                            str(PUBLIC / "poster.jpg"),
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    poster_set = True
            clips.append(dest)

        listing = tmp_path / "list.txt"
        listing.write_text("".join(f"file '{clip}'\n" for clip in clips), encoding="utf-8")
        dest = PUBLIC / "promo.mp4"
        subprocess.check_call(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c",
                "copy",
                str(dest),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        probe = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(dest)],
            text=True,
        ).strip()
        print(f"wrote {dest} ({dest.stat().st_size} bytes, {probe}s)")


if __name__ == "__main__":
    render()
