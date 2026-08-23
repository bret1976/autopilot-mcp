from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import (
    DEFAULT_BRAND_VOICE,
    DEFAULT_DAILY_HOUR,
    DEFAULT_DAILY_TIMEZONE,
    ONBOARD_PLATFORMS,
    STUDIO_NAME,
    STUDIO_WEBSITE,
)
from app.hashtags import normalize_tag

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META = re.compile(
    r'<meta[^>]+(?:name|property)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
_META_FLIP = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\']([^"\']+)["\']',
    re.I,
)
_SCRIPT = re.compile(r"<script[\s\S]*?</script>", re.I)
_STYLE = re.compile(r"<style[\s\S]*?</style>", re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_THIN_META = re.compile(
    r"website concept|lorem ipsum|coming soon|just a concept|placeholder",
    re.I,
)
_CAMEL = re.compile(r"([a-z])([A-Z])")
_WORD_PHRASE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[\s\-&]+[A-Za-z][A-Za-z0-9]*){0,3}")
_HOST_SUFFIXES = (
    "connects",
    "studio",
    "studios",
    "group",
    "media",
    "labs",
    "lab",
    "films",
    "film",
    "works",
    "digital",
    "agency",
    "coaching",
)


def hashtags_from_name(name: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    if not words:
        return []
    compact = normalize_tag("".join(words[:4]))
    return [compact] if compact else []


def _has_brand(record: dict[str, Any]) -> bool:
    return bool(
        str(record.get("brand_name") or "").strip()
        or str(record.get("website_url") or "").strip()
        or str(record.get("brand_voice") or "").strip()
    )


def _api_prompts() -> list[dict[str, str]]:
    return [
        {
            "id": "gemini_api_key",
            "prompt": "Paste your Gemini API key (https://aistudio.google.com/apikey).",
            "why": "Required to scan live trends and write this brand's copy.",
        },
        {
            "id": "postproxy_api_key",
            "prompt": "Paste your PostProxy API key (https://postproxy.dev).",
            "why": "Required to post through YOUR socials.",
        },
        {
            "id": "postproxy_profile_group_id",
            "prompt": "Paste your PostProxy profile group id.",
            "why": "Needed so OAuth connects LinkedIn / X / Instagram / YouTube / Facebook on your group.",
        },
    ]


def missing_fields(record: dict[str, Any]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    if not _has_brand(record):
        missing.append(
            {
                "id": "website_url",
                "prompt": (
                    "Paste the company website. That site becomes the brand of record "
                    f"(name + voice). {STUDIO_NAME} is only a placeholder until then."
                ),
                "why": "Copy must match the website they entered, not the initial studio brand.",
            }
        )
    for item in _api_prompts():
        if not str(record.get(item["id"]) or "").strip():
            missing.append(item)
    return missing


def readiness(record: dict[str, Any]) -> dict[str, Any]:
    missing = missing_fields(record)
    can_scan = not any(item["id"] == "gemini_api_key" for item in missing)
    can_publish = not any(
        item["id"] in {"postproxy_api_key", "postproxy_profile_group_id"} for item in missing
    )
    has_brand = not any(item["id"] == "website_url" for item in missing)
    ready = can_scan and can_publish and has_brand
    ask = [
        f"{i}. {item['prompt']} — {item['why']}" for i, item in enumerate(missing, start=1)
    ]
    if missing:
        if has_brand and not (can_scan and can_publish):
            brand = str(record.get("brand_name") or "this brand").strip()
            site = str(record.get("website_url") or "").strip()
            site_bit = f" from {site}" if site else ""
            say = (
                f"Brand is set ({brand}{site_bit}). That website is the brand of record — "
                "do not restore 6Frame Studio. Next step: paste your three APIs — "
                "Gemini API key, PostProxy API key, and PostProxy profile group id. "
                "I will not mock. Keys are never printed back.\n\n"
                + "\n".join(ask)
            )
        else:
            say = (
                "TrendPilot / Autopilot is connected, but it is not ready to scan or post. "
                "First the brand/website, then your three APIs "
                "(Gemini, PostProxy key, PostProxy profile group).\n\n"
                + "\n".join(ask)
                + "\n\nPaste those here. I will save them with setup (keys are never printed back), "
                "then send OAuth links so you can connect each network on your PostProxy account. "
                f"Default platforms: {', '.join(ONBOARD_PLATFORMS)}. "
                f"Daily automation is off until they turn it on. Default time is "
                f"{DEFAULT_DAILY_HOUR:02d}:00 {DEFAULT_DAILY_TIMEZONE} (8:00 AM PT)."
            )
    else:
        say = (
            "Keys and brand are in. Next I will send PostProxy OAuth links for each platform "
            "you want live. After you finish those, they can say Run Autopilot for a one-off, "
            "or call set_automation to turn on a daily scan → download → post. "
            "Ask whether they want require_approval=true (stage a draft, then approve_and_publish) "
            "or require_approval=false (auto-post, no click)."
        )
    return {
        "ready": ready,
        "can_scan": can_scan,
        "can_publish": can_publish,
        "has_brand": has_brand,
        "missing": [item["id"] for item in missing],
        "ask_the_user": missing,
        "say_to_user": say,
        "next_after_keys": [
            "Call postproxy_connect for linkedin, twitter, instagram, youtube, facebook.",
            "Open each returned URL and finish OAuth on the buyer's own accounts.",
            "Call run_autopilot with draft=true for the first live cut.",
            "Ask if they want daily automation. Then call set_automation.",
        ],
        "defaults": {
            "platforms": list(record.get("platforms") or ONBOARD_PLATFORMS),
            "daily_run_hour": record.get("daily_run_hour") or DEFAULT_DAILY_HOUR,
            "daily_run_timezone": record.get("daily_run_timezone") or DEFAULT_DAILY_TIMEZONE,
            "automation_enabled": False,
            "require_approval": True,
        },
    }


def blocked(record: dict[str, Any], *, need: str = "run") -> dict[str, Any] | None:
    report = readiness(record)
    if need == "scan" and report["can_scan"]:
        return None
    if need == "publish" and report["can_publish"]:
        return None
    if need == "run" and report["ready"]:
        return None
    return {
        "ok": False,
        "needs_setup": True,
        "mocked": False,
        "ask_the_user": report["ask_the_user"],
        "say_to_user": report["say_to_user"],
        "missing": report["missing"],
        "defaults": report["defaults"],
        "next_after_keys": report["next_after_keys"],
    }


def _meta_map(html: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in _META.findall(html):
        found[key.lower()] = value.strip()
    for value, key in _META_FLIP.findall(html):
        found.setdefault(key.lower(), value.strip())
    return found


def normalize_website(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    return text


def website_host(url: str) -> str:
    parsed = urlparse(normalize_website(url))
    host = (parsed.hostname or parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def website_slug(url: str) -> str:
    host = website_host(url)
    return host.split(".")[0] if host else ""


def is_studio_website(url: str) -> bool:
    return "6framestudio" in website_host(url)


def compact_brand(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def split_camel(word: str) -> str:
    return _CAMEL.sub(r"\1 \2", word).replace("-", " ").replace("_", " ")


def brand_name_from_host(url: str) -> str:
    """The pasted website is the brand. coryconnects.tech → Cory Connects."""
    host = website_host(url)
    if not host:
        return ""
    if "6framestudio" in host:
        return STUDIO_NAME
    slug = host.split(".")[0]
    for suffix in _HOST_SUFFIXES:
        if slug.lower().endswith(suffix) and len(slug) > len(suffix):
            head = slug[: -len(suffix)]
            return f"{head[:1].upper() + head[1:]} {suffix[:1].upper() + suffix[1:]}"
    spaced = slug.replace("-", " ").replace("_", " ")
    return " ".join(part[:1].upper() + part[1:] for part in spaced.split() if part)


def _is_thin(text: str) -> bool:
    clean = _WS.sub(" ", (text or "")).strip()
    if len(clean) < 60:
        return True
    return bool(_THIN_META.search(clean))


def visible_lines(html: str) -> list[str]:
    text = _SCRIPT.sub(" ", html or "")
    text = _STYLE.sub(" ", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|div|h1|h2|h3|h4|li|section|article)>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    text = html_lib.unescape(text)
    lines: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = _WS.sub(" ", raw).strip()
        if len(line) < 3 or line.lower() in seen:
            continue
        seen.add(line.lower())
        lines.append(line)
    return lines


def brand_mention_from_page(text: str, url: str) -> str:
    slug = compact_brand(website_slug(url))
    if not slug or len(slug) < 4:
        return ""
    for match in _WORD_PHRASE.finditer(text or ""):
        phrase = match.group(0).strip()
        compact = compact_brand(phrase)
        if compact == slug:
            return _WS.sub(" ", split_camel(phrase)).strip()
        if compact.startswith(slug) and len(compact) <= len(slug) + 12:
            head = phrase.split("-", 1)[0].split(" ", 1)[0] if "-" in phrase else phrase
            if compact_brand(head) == slug or compact_brand(split_camel(head).replace(" ", "")) == slug:
                return _WS.sub(" ", split_camel(head)).strip()
    return ""


def homepage_copy(lines: list[str], *, skip: str = "") -> str:
    skip_c = compact_brand(skip)
    chunks: list[str] = []
    for line in lines:
        if len(line) < 40:
            continue
        if skip_c and compact_brand(line) == skip_c:
            continue
        if _is_thin(line) and len(line) < 160:
            continue
        chunks.append(line)
        if sum(len(item) for item in chunks) >= 900:
            break
    return " ".join(chunks)


def brand_from_html(html: str, website: str) -> dict[str, Any]:
    """Website they pasted is the brand of record. Homepage body beats thin meta."""
    meta = _meta_map(html)
    title_match = _TITLE.search(html or "")
    title = _WS.sub(" ", title_match.group(1)).strip() if title_match else ""
    site = meta.get("og:site_name") or meta.get("application-name") or ""
    description = (
        meta.get("og:description")
        or meta.get("description")
        or meta.get("twitter:description")
        or ""
    )
    lines = visible_lines(html)
    page_text = "\n".join(lines)
    host_brand = brand_name_from_host(website)
    mentioned = brand_mention_from_page(page_text, website)
    brand_name = mentioned or host_brand or site or (
        title.split("|")[0].split("—")[0].split(" - ")[0].strip() if title else ""
    )
    body = homepage_copy(lines, skip=title)
    usable_meta = "" if _is_thin(description) else description
    source = body or usable_meta or title
    voice = (
        f"Write as {brand_name or 'this brand'}. "
        f"This website is the brand of record: {website}. "
        "Do not write as 6Frame Studio unless this brand is 6Frame. "
        "Match their public homepage — first person as this brand, not a growth desk, "
        "not thin meta copy, not the initial studio voice.\n\n"
        f"{source}"
    )
    return {
        "website_url": website,
        "brand_name": brand_name,
        "page_title": title,
        "description": description,
        "homepage_copy": body[:900],
        "brand_voice": voice[:2400],
        "brand_hashtags": hashtags_from_name(brand_name),
        "source": "homepage" if body else ("meta" if usable_meta else "title"),
    }


def is_studio_voice(voice: str) -> bool:
    text = (voice or "").strip()
    if not text:
        return False
    if text == DEFAULT_BRAND_VOICE:
        return True
    return "cinematic, precise, restrained" in text.lower() and "6frame" in text.lower()


async def fetch_brand_from_website(url: str) -> dict[str, Any]:
    website = normalize_website(url)
    if not website:
        return {"ok": False, "error": "A real website URL is required."}
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(
                website,
                headers={"User-Agent": "TrendPilot-MCP/1.0"},
            )
        html = response.text[:200_000]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not fetch {website}: {exc}", "website_url": website}

    parsed = brand_from_html(html, str(response.url) if getattr(response, "url", None) else website)
    parsed["ok"] = True
    parsed["website_url"] = website
    return parsed
