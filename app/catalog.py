"""Every platform PostProxy can publish to, with per-platform rules.

Each one is optional. The connected list is read from the buyer's PostProxy at runtime;
this catalog is the fallback and the source of the rules (caption limit, aspect, placement).
"""
from __future__ import annotations

from typing import Any

# id: label, aspect, caption_max, media_required, placement param, pin field,
#     placement_required, connect flow, hashtag caps
PLATFORM_CATALOG: dict[str, dict[str, Any]] = {
    "youtube": {"label": "YouTube", "aspect": "9:16", "caption_max": 5000, "title_max": 100,
                "media_required": True, "connect": "oauth", "hashtags": (8, 12)},
    "tiktok": {"label": "TikTok", "aspect": "9:16", "caption_max": 2200,
               "media_required": True, "connect": "oauth", "hashtags": (8, 12)},
    "instagram": {"label": "Instagram", "aspect": "9:16", "caption_max": 2200,
                  "media_required": True, "connect": "oauth", "hashtags": (8, 12)},
    "linkedin": {"label": "LinkedIn", "aspect": "16:9", "caption_max": 3000,
                 "placement_param": "organization_id", "pin_field": "linkedin_organization_id",
                 "placement_required": False, "connect": "oauth", "hashtags": (5, 8)},
    "twitter": {"label": "X", "aspect": "16:9", "caption_max": 280,
                "connect": "oauth", "hashtags": (1, 2)},
    "facebook": {"label": "Facebook", "aspect": "9:16", "caption_max": 63206,
                 "placement_param": "page_id", "pin_field": "facebook_page_id",
                 "placement_required": True, "connect": "oauth", "hashtags": (5, 8)},
    "threads": {"label": "Threads", "aspect": "9:16", "caption_max": 500,
                "connect": "oauth", "hashtags": (0, 1)},
    "pinterest": {"label": "Pinterest", "aspect": "9:16", "caption_max": 500,
                  "media_required": True, "placement_param": "board_id", "pin_field": "pinterest_board_id",
                  "placement_required": True, "connect": "oauth", "hashtags": (2, 5)},
    "bluesky": {"label": "Bluesky", "aspect": "9:16", "caption_max": 300,
                "connect": "app_password", "hashtags": (1, 2)},
    "telegram": {"label": "Telegram", "aspect": "9:16", "caption_max": 1024,
                 "placement_param": "chat_id", "pin_field": "telegram_chat_id",
                 "placement_required": True, "connect": "bot_token", "hashtags": (0, 3)},
    "google_business": {"label": "Google Business Profile", "aspect": "image", "caption_max": 1500,
                        "placement_param": "location_id", "pin_field": "google_location_id",
                        "placement_required": True, "connect": "dashboard", "hashtags": (0, 3)},
}

PLATFORM_ORDER = tuple(PLATFORM_CATALOG)


def platform_rules(name: str) -> dict[str, Any]:
    return PLATFORM_CATALOG.get(name, {})


def caption_max(name: str) -> int:
    return int(platform_rules(name).get("caption_max") or 0)


def clamp_caption(name: str, body: str) -> str:
    limit = caption_max(name)
    if not limit or len(body) <= limit:
        return body
    cut = body[: limit - 1].rstrip()
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def label(name: str) -> str:
    return str(platform_rules(name).get("label") or name)


def pin_fields() -> list[str]:
    return [r["pin_field"] for r in PLATFORM_CATALOG.values() if r.get("pin_field")]
