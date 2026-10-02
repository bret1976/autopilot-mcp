"""Run Guard Pack — one live publish per buyer+content fingerprint within a window.

Idea inspiration (no code copied):
- Matthew-Selvam/Open-Dispatch (MIT) — queue/state so the same unit is not
  re-published while already published.
- ShadowSlayer03/Post4U-Schedule-Social-Media-Posts (MIT) — successful
  platforms never re-post.
- theexperiencecompany/gaia processed_webhooks (concept) — ledger of seen keys.
- hookdeck/webhook-skills (HN) — webhook/idempotency best-practice ideas.

Original autopilot-mcp Python: buyer-scoped content fingerprints from platforms
+ normalized captions + media URLs, sliding ledger on DATA_DIR, soft-block
duplicate live (non-draft) publish_cut when RUN_GUARD_BLOCK is on (default on).
No UI/screens — health packs marker + summary/check APIs only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from app.config import data_dir

PACK = "run-guard-v1"
DEFAULT_WINDOW_SEC = 24 * 60 * 60  # 24 hours
DEFAULT_MAX_LEDGER = 800
NEAR_DUPE_THRESHOLD = 0.88

_lock = threading.Lock()


def _truthy(name: str, default: str = "1") -> bool:
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    return (raw or "").strip().lower() not in {"0", "false", "no", "off", ""}


def window_sec() -> int:
    try:
        return max(60, int(os.environ.get("RUN_GUARD_WINDOW_SEC") or DEFAULT_WINDOW_SEC))
    except ValueError:
        return DEFAULT_WINDOW_SEC


def blocking_enabled() -> bool:
    """When true, live publish soft-skips duplicates. Default on."""
    return _truthy("RUN_GUARD_BLOCK", "1")


def _ledger_path() -> Path:
    override = os.environ.get("RUN_GUARD_LEDGER")
    if override:
        return Path(override)
    return data_dir() / "run_guard_ledger.json"


def _norm_token(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _platforms_key(platforms: Any) -> str:
    if isinstance(platforms, list):
        parts = [str(p).strip().lower() for p in platforms if str(p).strip()]
    elif platforms:
        parts = [
            p.strip().lower()
            for p in str(platforms).replace("|", ",").split(",")
            if p.strip()
        ]
    else:
        parts = []
    return ",".join(sorted(set(parts))) or "unknown"


def _caption_blob(copy: dict[str, Any] | None) -> str:
    if not copy:
        return ""
    captions = copy.get("captions") if isinstance(copy.get("captions"), dict) else {}
    parts = [_norm_token(str(v)) for v in captions.values() if v]
    title = _norm_token(str(copy.get("title") or copy.get("youtube_title") or ""))
    if title:
        parts.insert(0, title)
    return "\n".join(p for p in parts if p)


def _media_key(media: dict[str, Any] | None) -> str:
    if not media:
        return ""
    bits: list[str] = []
    for key in ("vertical_url", "landscape_url", "poster_url", "source_url", "media_url"):
        val = media.get(key)
        if not val:
            continue
        s = str(val)
        name = Path(s.split("?", 1)[0]).name if ("/" in s or "\\" in s) else s
        bits.append(_norm_token(name))
    return "|".join(bits)


def content_fingerprint(
    *,
    buyer_id: str,
    platforms: Any,
    copy: dict[str, Any] | None,
    media: dict[str, Any] | None,
) -> str | None:
    """Stable key for one live outbound unit (buyer + platforms + caption + media)."""
    buyer = _norm_token(buyer_id) or "unknown"
    plats = _platforms_key(platforms)
    caption = _caption_blob(copy)
    media_k = _media_key(media)
    if not caption and not media_k:
        return None
    material = f"{buyer}\n{plats}\n{caption}\n{media_k}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:20]
    return f"{buyer}|{plats}|{digest}"


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", _norm_token(text))
    return {w for w in words if len(w) > 1}


def caption_similarity(a: str, b: str) -> float:
    """Jaccard token overlap on normalized captions (0..1)."""
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta and not tb:
        return 1.0 if _norm_token(a) == _norm_token(b) and a else 0.0
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _load_ledger() -> list[dict[str, Any]]:
    path = _ledger_path()
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    except Exception:
        return []
    return []


def _save_ledger(rows: list[dict[str, Any]]) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _prune(rows: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    window = window_sec()
    kept = [
        r
        for r in rows
        if isinstance(r.get("ts"), (int, float)) and (now - float(r["ts"])) <= window
    ]
    if len(kept) > DEFAULT_MAX_LEDGER:
        kept = kept[-DEFAULT_MAX_LEDGER:]
    return kept


def summary() -> dict[str, Any]:
    with _lock:
        rows = _prune(_load_ledger())
        if _ledger_path().exists() or rows:
            _save_ledger(rows)
    return {
        "pack": PACK,
        "blocking": blocking_enabled(),
        "window_sec": window_sec(),
        "ledger_size": len(rows),
        "near_dupe_threshold": NEAR_DUPE_THRESHOLD,
        "ledger_path": str(_ledger_path()),
    }


def check_publish(
    *,
    buyer_id: str,
    platforms: Any = None,
    copy: dict[str, Any] | None = None,
    media: dict[str, Any] | None = None,
    force: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Return whether a live publish would duplicate a recent send for this buyer."""
    now = time.time() if now is None else now
    fp = content_fingerprint(
        buyer_id=buyer_id, platforms=platforms, copy=copy, media=media
    )
    caption = _caption_blob(copy)
    plats = _platforms_key(platforms)
    result: dict[str, Any] = {
        "pack": PACK,
        "buyer_id": _norm_token(buyer_id) or buyer_id,
        "fingerprint": fp,
        "platforms": plats,
        "blocked": False,
        "force": bool(force),
        "blocking_enabled": blocking_enabled(),
        "reason": None,
        "match": None,
        "near_score": None,
    }
    if not fp:
        result["reason"] = "no_fingerprint"
        return result
    if force:
        result["reason"] = "forced"
        return result

    buyer = result["buyer_id"]
    with _lock:
        rows = _prune(_load_ledger(), now=now)
        exact = next(
            (
                r
                for r in reversed(rows)
                if r.get("fingerprint") == fp and r.get("buyer_id") == buyer
            ),
            None,
        )
        if exact:
            result["match"] = {
                "fingerprint": exact.get("fingerprint"),
                "buyer_id": exact.get("buyer_id"),
                "ts": exact.get("ts"),
                "age_sec": round(now - float(exact["ts"]), 1),
            }
            if blocking_enabled():
                result["blocked"] = True
                result["reason"] = "duplicate_fingerprint"
            else:
                result["reason"] = "duplicate_fingerprint_soft"
            return result

        best_score = 0.0
        best_row = None
        for row in reversed(rows):
            if row.get("buyer_id") != buyer:
                continue
            row_plats = set((row.get("platforms") or "").split(",")) - {""}
            post_plats = set(plats.split(",")) - {""}
            if row_plats and post_plats and row_plats.isdisjoint(post_plats):
                continue
            score = caption_similarity(caption, str(row.get("caption") or ""))
            if score > best_score:
                best_score = score
                best_row = row
        result["near_score"] = round(best_score, 4)
        if best_row and best_score >= NEAR_DUPE_THRESHOLD and caption:
            result["match"] = {
                "fingerprint": best_row.get("fingerprint"),
                "buyer_id": best_row.get("buyer_id"),
                "ts": best_row.get("ts"),
                "age_sec": round(now - float(best_row["ts"]), 1),
                "near_score": round(best_score, 4),
            }
            if blocking_enabled():
                result["blocked"] = True
                result["reason"] = "near_duplicate_caption"
            else:
                result["reason"] = "near_duplicate_caption_soft"
            return result

    result["reason"] = "ok"
    return result


def record_publish(
    *,
    buyer_id: str,
    platforms: Any = None,
    copy: dict[str, Any] | None = None,
    media: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Append a successful live publish fingerprint to the ledger."""
    now = time.time() if now is None else now
    fp = content_fingerprint(
        buyer_id=buyer_id, platforms=platforms, copy=copy, media=media
    )
    if not fp:
        return {"pack": PACK, "recorded": False, "reason": "no_fingerprint"}
    row = {
        "fingerprint": fp,
        "buyer_id": _norm_token(buyer_id) or buyer_id,
        "platforms": _platforms_key(platforms),
        "caption": _caption_blob(copy)[:500],
        "media": _media_key(media),
        "ts": now,
    }
    with _lock:
        rows = _prune(_load_ledger(), now=now)
        rows.append(row)
        rows = _prune(rows, now=now)
        _save_ledger(rows)
    return {"pack": PACK, "recorded": True, "fingerprint": fp, "ledger_size": len(rows)}
