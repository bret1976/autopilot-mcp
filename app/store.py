from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import (
    DEFAULT_DAILY_HOUR,
    DEFAULT_DAILY_TIMEZONE,
    ONBOARD_PLATFORMS,
    OWNER_BUYER_ID,
    STUDIO_NAME,
    data_dir,
)
from app.onboard import brand_name_from_host, hashtags_from_name, is_studio_voice, is_studio_website
from app.platforms import normalize_platforms
from app.tokens import mint_token


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _buyer_path(buyer_id: str) -> Path:
    return data_dir() / "buyers" / f"{buyer_id}.json"


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_buyer(buyer_id: str) -> dict[str, Any] | None:
    path = _buyer_path(buyer_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_buyer(
    buyer_id: str,
    *,
    email: str = "",
    note: str = "",
    token: str | None = None,
) -> dict[str, Any]:
    existing = load_buyer(buyer_id)
    if existing:
        if email and not existing.get("email"):
            existing["email"] = email
            existing["updated_at"] = _now()
            _write(_buyer_path(buyer_id), existing)
        return existing
    record = {
        "buyer_id": buyer_id,
        "email": email,
        "note": note,
        "created_at": _now(),
        "updated_at": _now(),
        "token": token or mint_token(buyer_id),
        "gemini_api_key": "",
        "postproxy_api_key": "",
        "postproxy_profile_group_id": "",
        "facebook_page_id": "",
        "google_location_id": "",
        "brand_name": "",
        "website_url": "",
        "brand_voice": "",
        "brand_hashtags": [],
        "platforms": list(ONBOARD_PLATFORMS),
        "daily_run_hour": DEFAULT_DAILY_HOUR,
        "daily_run_timezone": DEFAULT_DAILY_TIMEZONE,
        "automation_enabled": False,
        "require_approval": True,
        "last_automation_date": "",
        "public_base_url": "",
        "last_run": None,
    }
    _write(_buyer_path(buyer_id), record)
    return apply_owner_studio_brand(record)


def reset_instance(buyer_id: str) -> dict[str, Any]:
    """Brand-new run on the same license. Drops leftover companies, keys, drafts, and daily jobs."""
    record = load_buyer(buyer_id) or ensure_buyer(buyer_id)
    record["gemini_api_key"] = ""
    record["postproxy_api_key"] = ""
    record["postproxy_profile_group_id"] = ""
    record["facebook_page_id"] = ""
    record["google_location_id"] = ""
    record["brand_name"] = ""
    record["website_url"] = ""
    record["brand_voice"] = ""
    record["brand_hashtags"] = []
    record["platforms"] = list(ONBOARD_PLATFORMS)
    record["automation_enabled"] = False
    record["require_approval"] = False
    record["last_automation_date"] = ""
    record["last_run"] = None
    return save_buyer(record)


def should_start_new_instance(record: dict[str, Any]) -> bool:
    """True when this license still has a previous company's draft or daily job."""
    return bool(record.get("last_run") or record.get("automation_enabled"))


def apply_owner_studio_brand(record: dict[str, Any]) -> dict[str, Any]:
    """Never keep IAN. Never invent 6Frame over an empty or pasted-website license."""
    if str(record.get("buyer_id") or "") != OWNER_BUYER_ID:
        return record
    name = str(record.get("brand_name") or "").strip()
    website = str(record.get("website_url") or "").strip()
    website_l = website.lower()
    is_ian = "iangroup" in website_l or name.upper().replace(" ", "").startswith("IAN")
    if is_ian:
        record["brand_name"] = ""
        record["website_url"] = ""
        record["brand_voice"] = ""
        record["brand_hashtags"] = []
        return save_buyer(record)
    if website and not is_studio_website(website):
        changed = False
        if not name or name == STUDIO_NAME:
            record["brand_name"] = brand_name_from_host(website) or name
            changed = True
        voice = str(record.get("brand_voice") or "").strip()
        if not voice or is_studio_voice(voice):
            brand = record["brand_name"] or brand_name_from_host(website)
            record["brand_voice"] = (
                f"Write as {brand}. This website is the brand of record: {website}. "
                "Do not write as 6Frame Studio unless this brand is 6Frame. "
                "Match the public homepage."
            )
            changed = True
        if not record.get("brand_hashtags"):
            record["brand_hashtags"] = hashtags_from_name(str(record.get("brand_name") or ""))
            changed = True
        return save_buyer(record) if changed else record
    return record


def clear_buyer_api_keys(buyer_id: str) -> dict[str, Any]:
    """Drop Gemini and PostProxy keys so onboard asks for the three APIs again."""
    record = load_buyer(buyer_id) or ensure_buyer(buyer_id)
    record["gemini_api_key"] = ""
    record["postproxy_api_key"] = ""
    record["postproxy_profile_group_id"] = ""
    return save_buyer(record)


def save_buyer(record: dict[str, Any]) -> dict[str, Any]:
    record["updated_at"] = _now()
    if "platforms" in record:
        record["platforms"] = normalize_platforms(record.get("platforms") or [])
    _write(_buyer_path(record["buyer_id"]), record)
    return record


def update_setup(buyer_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    record = load_buyer(buyer_id) or ensure_buyer(buyer_id)
    secret_keys = {"gemini_api_key", "postproxy_api_key"}
    for key, value in fields.items():
        if value is None:
            continue
        if key in secret_keys:
            text = str(value).strip()
            if text:
                record[key] = text
            continue
        if key == "platforms":
            record[key] = normalize_platforms(value if isinstance(value, list) else [value])
            continue
        if key == "brand_hashtags":
            if isinstance(value, str):
                value = [part.strip() for part in value.replace(",", " ").split() if part.strip()]
            record[key] = [str(tag) for tag in (value or []) if str(tag).strip()]
            continue
        if key == "daily_run_hour":
            try:
                hour = int(value)
            except (TypeError, ValueError):
                continue
            record[key] = max(0, min(hour, 23))
            continue
        if key in {"automation_enabled", "require_approval"}:
            fallback = True if key == "require_approval" else False
            record[key] = parse_bool(value, parse_bool(record.get(key), fallback))
            continue
        if key in {
            "brand_voice",
            "brand_name",
            "website_url",
            "daily_run_timezone",
            "postproxy_profile_group_id",
            "public_base_url",
            "email",
            "note",
            "facebook_page_id",
            "google_location_id",
            "last_automation_date",
        }:
            record[key] = str(value).strip()
    return save_buyer(record)


def set_last_run(buyer_id: str, last_run: dict[str, Any]) -> dict[str, Any]:
    record = load_buyer(buyer_id) or ensure_buyer(buyer_id)
    record["last_run"] = last_run
    return save_buyer(record)


def public_config(record: dict[str, Any]) -> dict[str, Any]:
    from app.onboard import readiness

    report = readiness(record)
    return {
        "buyer_id": record.get("buyer_id"),
        "email": record.get("email") or None,
        "brand_name": record.get("brand_name") or None,
        "website_url": record.get("website_url") or None,
        "brand_voice": record.get("brand_voice") or None,
        "brand_hashtags": record.get("brand_hashtags") or [],
        "platforms": record.get("platforms") or list(ONBOARD_PLATFORMS),
        "daily_run_hour": record.get("daily_run_hour") or DEFAULT_DAILY_HOUR,
        "daily_run_timezone": record.get("daily_run_timezone") or DEFAULT_DAILY_TIMEZONE,
        "automation_enabled": bool(record.get("automation_enabled")),
        "require_approval": record.get("require_approval") is not False,
        "last_automation_date": record.get("last_automation_date") or None,
        "pending_approval": bool((record.get("last_run") or {}).get("pending_approval")),
        "public_base_url": record.get("public_base_url") or None,
        "postproxy_profile_group_id": record.get("postproxy_profile_group_id") or None,
        "facebook_page_id": record.get("facebook_page_id") or None,
        "google_location_id": record.get("google_location_id") or None,
        "gemini_key": _mask(record.get("gemini_api_key")),
        "postproxy_key": _mask(record.get("postproxy_api_key")),
        "configured": report["ready"],
        "ready": report["ready"],
        "missing": report["missing"],
        "updated_at": record.get("updated_at"),
        "last_run": record.get("last_run"),
    }


def _mask(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 4:
        return "••••"
    return f"••••{secret[-4:]}"


def append_lead(payload: dict[str, Any]) -> dict[str, Any]:
    lead = {
        "id": secrets.token_hex(8),
        "created_at": _now(),
        **payload,
    }
    path = data_dir() / "leads.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(lead) + "\n")
    return lead


def list_leads(limit: int = 50) -> list[dict[str, Any]]:
    path = data_dir() / "leads.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return list(reversed(rows[-limit:]))


def iter_buyer_records() -> list[dict[str, Any]]:
    folder = data_dir() / "buyers"
    out: list[dict[str, Any]] = []
    if not folder.exists():
        return out
    for path in sorted(folder.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return out


def list_buyers() -> list[dict[str, Any]]:
    return [public_config(record) for record in iter_buyer_records()]
