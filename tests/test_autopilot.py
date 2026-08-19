from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-tests")

import pytest

from app.spine import run_autopilot
from app.store import ensure_buyer, public_config, save_buyer


@pytest.mark.asyncio
async def test_run_autopilot_mock_path() -> None:
    record = ensure_buyer("mock-studio", email="mock@studio.test")
    record["brand_name"] = "IAN Group"
    record["brand_hashtags"] = ["#IANGroup"]
    record["platforms"] = ["instagram", "youtube", "facebook", "linkedin", "twitter"]
    record = save_buyer(record)
    result = await run_autopilot(record, niche="AI filmmaking", mock=True)
    assert result["ok"] is True
    assert result["mocked"] is True
    assert result["scan"]["source_url"]
    assert result["media"]["vertical"].endswith("9x16.mp4")
    assert result["media"]["landscape"].endswith("16x9.mp4")
    captions = result["copy"]["captions"]
    assert "#IANGroup" in captions["instagram"]
    assert "#6FrameStudio" not in captions["instagram"]
    assert "#Shorts" in result["copy"]["youtube_title"]
    assert "instagram" in result["publish"]["batches"]["vertical_9x16"]
    assert "linkedin" in result["publish"]["batches"]["landscape_16x9"]
    public = public_config(record)
    assert public["gemini_key"] is None
    assert "gemini_api_key" not in public
