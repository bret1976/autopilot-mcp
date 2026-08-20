from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import GEMINI_COPY_MODELS, GEMINI_TIMEOUT_SECONDS

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.S)


async def generate_json(
    api_key: str,
    prompt: str,
    *,
    grounded: bool = False,
    models: tuple[str, ...] = GEMINI_COPY_MODELS,
    timeout: float = GEMINI_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    text = await generate_text(
        api_key,
        prompt,
        grounded=grounded,
        models=models,
        timeout=timeout,
    )
    return parse_json_object(text)


async def generate_text(
    api_key: str,
    prompt: str,
    *,
    grounded: bool = False,
    models: tuple[str, ...] = GEMINI_COPY_MODELS,
    timeout: float = GEMINI_TIMEOUT_SECONDS,
) -> str:
    if not api_key:
        raise RuntimeError("Gemini API key is not configured. Call setup first.")
    errors: list[str] = []
    for model in models:
        try:
            return await _call_model(
                api_key,
                model,
                prompt,
                grounded=grounded,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 — try the next Gemini model
            errors.append(f"{model}: {exc}")
    raise RuntimeError("Gemini request failed. " + " | ".join(errors))


async def _call_model(
    api_key: str,
    model: str,
    prompt: str,
    *,
    grounded: bool,
    timeout: float,
) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload: dict[str, Any] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4},
    }
    if grounded:
        payload["tools"] = [{"google_search": {}}]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                headers={"x-goog-api-key": api_key, "content-type": "application/json"},
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"timeout after {int(timeout)}s") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code} {response.text[:400]}")
    data = response.json()
    parts = (
        data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [])
    )
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    return text


def parse_json_object(text: str) -> dict[str, Any]:
    fenced = _JSON_FENCE.search(text)
    raw = fenced.group(1) if fenced else text
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError("Gemini did not return JSON")
    return json.loads(raw[start : end + 1])
