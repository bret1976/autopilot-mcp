from __future__ import annotations

import re

from app.config import HASHTAG_CAPS, LOCKED_HASHTAGS

_TAG_RE = re.compile(r"^#?[A-Za-z][A-Za-z0-9_]{0,48}$")
_PAD = (
    "#GenerativeFilm",
    "#CinematicAI",
    "#AIVideo",
    "#ShortFilm",
    "#DirectorNotes",
    "#FilmCraft",
)


def normalize_tag(tag: str) -> str | None:
    raw = tag.strip()
    if not raw:
        return None
    if not raw.startswith("#"):
        raw = f"#{raw}"
    raw = "#" + re.sub(r"[^A-Za-z0-9_]", "", raw[1:])
    if not _TAG_RE.match(raw):
        return None
    return raw


def topic_tags(tags: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for tag in tags or []:
        clean = normalize_tag(str(tag))
        if clean and clean not in out and clean not in LOCKED_HASHTAGS:
            out.append(clean)
    return out


def hashtags_for(
    platform: str,
    extra: list[str] | tuple[str, ...] | None = None,
    locked: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    key = platform.strip().lower()
    if key == "x":
        key = "twitter"
    if key in {"gbp", "gmb", "google_business_profile"}:
        key = "google_business"
    lo, hi = HASHTAG_CAPS.get(key, (0, 3))
    extras = topic_tags(extra)
    locked_tags = list(LOCKED_HASHTAGS if locked is None else locked)

    if key == "google_business":
        # GBP local posts look like a shop update, not a hashtag dump.
        chosen = topic_tags(list(locked or []) + extras)
        return chosen[:hi]

    if key == "twitter":
        # X: 1–2 tags, last tweet stays under 280. Prefer studio mark, then a topic.
        chosen = extras[:1]
        if not chosen and locked_tags:
            chosen = [locked_tags[0]]
        if hi >= 2 and extras[1:]:
            chosen.append(extras[1])
        elif hi >= 2 and len(chosen) < 2 and len(locked_tags) > 1:
            chosen.append(locked_tags[1])
        return chosen[:hi]

    tags = list(locked_tags)
    for tag in extras:
        if tag not in tags:
            tags.append(tag)
    for pad in _PAD:
        if len(tags) >= lo:
            break
        if pad not in tags:
            tags.append(pad)
    return tags[:hi]


def apply_hashtags(
    platform: str,
    body: str,
    extra: list[str] | tuple[str, ...] | None = None,
    *,
    twitter_limit: int = 280,
    locked: tuple[str, ...] | list[str] | None = None,
) -> str:
    tags = hashtags_for(platform, extra, locked=locked)
    tag_line = " ".join(tags)
    text = (body or "").rstrip()
    key = platform.strip().lower()
    if key in {"gbp", "gmb", "google_business_profile"}:
        key = "google_business"
    if not tags:
        return text
    if key in {"twitter", "x"}:
        candidate = f"{text} {tag_line}".strip() if text else tag_line
        if len(candidate) <= twitter_limit:
            return candidate
        room = twitter_limit - len(tag_line) - 1
        if room < 8:
            return tag_line[:twitter_limit]
        return f"{text[:room].rstrip()} {tag_line}"
    if not text:
        return tag_line
    if any(tag.lower() in text.lower() for tag in tags):
        missing = [tag for tag in tags if tag.lower() not in text.lower()]
        return f"{text} {' '.join(missing)}".rstrip()
    return f"{text}\n\n{tag_line}"
