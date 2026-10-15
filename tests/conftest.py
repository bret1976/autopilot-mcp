import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("ADMIN_SECRET", "test-admin")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-tests")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
# Legacy scan/transform tests run with originals off; tests/test_originals.py turns them on explicitly.
os.environ.setdefault("GROWTH_V2_ORIGINALS", "0")
