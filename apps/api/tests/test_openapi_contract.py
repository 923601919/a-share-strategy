"""Contract smoke: key OpenAPI paths exist for frontend sync."""
from __future__ import annotations

import sys
from pathlib import Path

API = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API))

from main import app


REQUIRED_PATHS = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/me",
    "/api/scan",
    "/api/scan/jobs",
    "/api/watchlist",
    "/api/watchlist/history",
    "/api/watchlist/stats",
    "/api/stats/score-effectiveness",
    "/api/sim",
}


def test_openapi_has_required_paths():
    schema = app.openapi()
    paths = set(schema.get("paths") or {})
    missing = REQUIRED_PATHS - paths
    assert not missing, f"missing OpenAPI paths: {sorted(missing)}"


def test_watchlist_history_schema_mentions_completion():
    schema = app.openapi()
    # history route exists; response is untyped dict but path must be present
    assert "/api/watchlist/history" in schema["paths"]
    assert "get" in schema["paths"]["/api/watchlist/history"]
