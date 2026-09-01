"""Auth + per-user isolation smoke tests."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

API = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API))

# Isolate DB / auth before importing app modules
_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _db
os.environ["JWT_SECRET"] = "test-secret-for-pytest"
os.environ["AUTH_REQUIRED"] = "true"
os.environ["DEMO_MODE"] = "true"
os.environ["DOCS_ENABLED"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from config import settings  # noqa: E402
from db import add_watch, init_db, list_watchlist  # noqa: E402
from main import app  # noqa: E402
from user_ctx import user_scope  # noqa: E402

settings.db_path = Path(_db)
settings.jwt_secret = "test-secret-for-pytest"
settings.auth_required = True
settings.demo_mode = True

init_db()
client = TestClient(app)


def test_unauthenticated_watchlist_401():
    r = client.get("/api/watchlist")
    assert r.status_code == 401


def test_bootstrap_login_invite_isolation():
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin1", "password": "secret12"},
    )
    assert r.status_code == 200, r.text
    admin_token = r.json()["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    inv = client.post("/api/auth/invites", headers=admin_headers, json={"note": "friend"})
    assert inv.status_code == 200, inv.text
    code = inv.json()["code"]

    reg = client.post(
        "/api/auth/register",
        json={"username": "friend1", "password": "secret12", "invite_code": code},
    )
    assert reg.status_code == 200, reg.text
    friend_token = reg.json()["access_token"]
    friend_headers = {"Authorization": f"Bearer {friend_token}"}

    # admin adds watch
    with user_scope(1):
        add_watch("600000", "浦发银行", source="manual")
        assert any(x["code"] == "600000" for x in list_watchlist())

    # friend list empty
    wl = client.get("/api/watchlist", headers=friend_headers)
    assert wl.status_code == 200
    assert wl.json()["items"] == [] or all(i["code"] != "600000" for i in wl.json()["items"])

    # friend cannot see admin job ids they didn't create
    job = client.post("/api/scan/jobs", headers=admin_headers, json={"mode": "fenshi"})
    assert job.status_code == 200
    jid = job.json()["job_id"]
    other = client.get(f"/api/scan/jobs/{jid}", headers=friend_headers)
    assert other.status_code == 404


def test_auth_status():
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_required"] is True
