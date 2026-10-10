from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-name-tests")

from fastapi.testclient import TestClient

from app.main import app

OLD = ("6Frame Autopilot", "Autopilot MCP")


def test_user_facing_name_is_trendpilot() -> None:
    with TestClient(app) as client:
        for path in ("/", "/buy", "/llms.txt", "/agent-terms.md",
                     "/.well-known/mcp/server-card.json", "/openapi.json"):
            res = client.get(path)
            assert res.status_code == 200, path
            for old in OLD:
                assert old not in res.text, (path, old)
        assert client.get("/openapi.json").json()["info"]["title"] == "TrendPilot"
        assert "TrendPilot" in client.get("/").text
        card = client.get("/.well-known/mcp/server-card.json").json()
        assert "TrendPilot" in str(card)
