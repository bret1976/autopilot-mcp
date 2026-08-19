from __future__ import annotations

import os
from pathlib import Path

# Single source of truth. Landing, /buy, /admin, and CTAs read this.
PRICE_USD = 997
PRICE_LABEL = f"${PRICE_USD} once"
CTA_LABEL = f"Get the link · ${PRICE_USD} once"
CTA_ACCESS = f"Get access · ${PRICE_USD}"
CTA_PRICE = f"Get your MCP link · ${PRICE_USD}"

PRODUCT_NAME = "6Frame Autopilot"
STUDIO_NAME = "6Frame Studio"
OWNER_NAME = "Bret Jenny"

LOCKED_HASHTAGS = ("#6FrameStudio", "#AIFilmmaking", "#AICinema")

DEFAULT_PLATFORMS = (
    "instagram",
    "tiktok",
    "youtube",
    "facebook",
    "linkedin",
    "twitter",
)

# What a new buyer gets until they change it in setup.
ONBOARD_PLATFORMS = (
    "linkedin",
    "twitter",
    "instagram",
    "youtube",
    "facebook",
)

DEFAULT_DAILY_HOUR = 8
DEFAULT_DAILY_TIMEZONE = "America/Los_Angeles"

VERTICAL_PLATFORMS = frozenset({"instagram", "tiktok", "youtube", "facebook"})
LANDSCAPE_PLATFORMS = frozenset({"linkedin", "twitter", "x"})

HASHTAG_CAPS: dict[str, tuple[int, int]] = {
    "linkedin": (5, 8),
    "facebook": (5, 8),
    "instagram": (8, 12),
    "tiktok": (8, 12),
    "youtube": (8, 12),
    "twitter": (1, 2),
    "x": (1, 2),
}

DEFAULT_BRAND_VOICE = (
    "Cinematic, precise, restrained. Write like a studio director, not a growth desk. "
    "Never use: game-changer, revolutionize, unlock, next-level, crush, viral hack. "
    "Prefer craft language: frame, cut, light, tempo, voice."
)

GEMINI_MODELS = (
    "gemini-3.1-pro-preview",
    "gemini-3.6-flash",
)

MAX_CLIP_SECONDS = 59
POSTPROXY_API_BASE = "https://api.postproxy.dev"
THEORY_VIDEO_URL = "https://www.youtube.com/watch?v=x0zH8b7KqJ8"

ROOT_DIR = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT_DIR / "public"
TEMPLATES_DIR = ROOT_DIR / "templates"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def issuer_secret() -> str:
    secret = _env("MCP_ISSUER_SECRET")
    if secret:
        return secret
    if _env("APP_ENV", "development") == "production":
        raise RuntimeError("MCP_ISSUER_SECRET is required in production")
    return "dev-issuer-secret-not-for-production"


def admin_secret() -> str:
    return _env("ADMIN_SECRET", "dev-admin-secret")


def public_base_url() -> str:
    return _env("PUBLIC_BASE_URL", "http://127.0.0.1:8080").rstrip("/")


def data_dir() -> Path:
    path = Path(_env("DATA_DIR", str(ROOT_DIR / "data"))).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    (path / "buyers").mkdir(parents=True, exist_ok=True)
    (path / "media").mkdir(parents=True, exist_ok=True)
    return path


def stripe_payment_link() -> str:
    return _env("STRIPE_PAYMENT_LINK")


def stripe_secret_key() -> str:
    return _env("STRIPE_SECRET_KEY")


def revoked_keys() -> set[str]:
    raw = _env("REVOKED_KEYS")
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def ytdlp_cookies_file() -> Path | None:
    """Optional Netscape cookies so YouTube stops treating Railway as a bot."""
    path = _env("YTDLP_COOKIES_FILE") or _env("YOUTUBE_COOKIES_FILE")
    if path:
        candidate = Path(path).expanduser()
        if candidate.exists():
            return candidate
    raw = _env("YTDLP_COOKIES_B64")
    if not raw:
        return None
    dest = data_dir() / "youtube.cookies"
    try:
        import base64

        dest.write_bytes(base64.b64decode(raw))
        dest.chmod(0o600)
        return dest
    except Exception:  # noqa: BLE001
        return None
