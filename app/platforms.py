from __future__ import annotations

from app.config import LANDSCAPE_PLATFORMS, VERTICAL_PLATFORMS

ALIASES = {
    "ig": "instagram",
    "insta": "instagram",
    "yt": "youtube",
    "youtube shorts": "youtube",
    "youtube_shorts": "youtube",
    "shorts": "youtube",
    "fb": "facebook",
    "x": "twitter",
    "twitter/x": "twitter",
    "twitter": "twitter",
    "li": "linkedin",
}


def normalize_platform(name: str) -> str:
    key = name.strip().lower()
    return ALIASES.get(key, key.replace(" ", "_"))


def normalize_platforms(platforms: list[str] | tuple[str, ...] | None) -> list[str]:
    if not platforms:
        return []
    out: list[str] = []
    for raw in platforms:
        name = normalize_platform(str(raw))
        if name and name not in out:
            out.append(name)
    return out


def aspect_for(platform: str) -> str:
    name = normalize_platform(platform)
    if name in LANDSCAPE_PLATFORMS:
        return "16:9"
    if name in VERTICAL_PLATFORMS:
        return "9:16"
    raise ValueError(f"Unknown platform: {platform}")


def split_batches(platforms: list[str]) -> dict[str, list[str]]:
    vertical: list[str] = []
    landscape: list[str] = []
    unknown: list[str] = []
    for raw in platforms:
        name = normalize_platform(raw)
        if name in VERTICAL_PLATFORMS:
            vertical.append(name)
        elif name in LANDSCAPE_PLATFORMS:
            landscape.append(name)
        else:
            unknown.append(name)
    return {"vertical_9x16": vertical, "landscape_16x9": landscape, "unknown": unknown}


def youtube_title(title: str) -> str:
    text = (title or "").strip() or "Untitled cut"
    if "#shorts" not in text.lower():
        text = f"{text} #Shorts"
    return text[:100]
