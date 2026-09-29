"""End-to-end against the Firebase Auth + Firestore emulators — nothing about
authentication is mocked: real ID tokens from the Auth emulator, verified by
firebase_admin, profiles read from Firestore by the production code path.

Runs only when the emulators are up:
    FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1:9099 FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 \
    FIREBASE_PROJECT_ID=demo-elite pytest tests/test_emulator_integration.py
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.auth as auth_mod
import app.common as common
from app.main import create_app
from tests.conftest import fake_verify_email

AUTH = os.getenv("FIREBASE_AUTH_EMULATOR_HOST")
STORE = os.getenv("FIRESTORE_EMULATOR_HOST")
PROJECT = os.getenv("FIREBASE_PROJECT_ID", "demo-elite")

pytestmark = pytest.mark.skipif(not (AUTH and STORE), reason="Firebase emulators not running")


def _post(url: str, body: dict[str, Any], headers: dict[str, str] | None = None, method: str = "POST") -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method=method, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b"{}")


def _user(profile: dict[str, Any] | None) -> tuple[str, str]:
    email = f"{uuid.uuid4().hex[:10]}@elite.test"
    base = f"http://{AUTH}/identitytoolkit.googleapis.com/v1"
    uid = _post(f"{base}/accounts:signUp?key=any", {"email": email, "password": "password123", "returnSecureToken": True})["localId"]
    if profile is not None:
        fields = {k: ({"booleanValue": v} if isinstance(v, bool) else {"mapValue": {"fields": {"status": {"stringValue": v["status"]}}}} if isinstance(v, dict) else {"stringValue": v}) for k, v in profile.items()}
        _post(f"http://{STORE}/v1/projects/{PROJECT}/databases/(default)/documents/users/{uid}", {"fields": fields}, {"Authorization": "Bearer owner"}, method="PATCH")
    token = _post(f"{base}/accounts:signInWithPassword?key=any", {"email": email, "password": "password123", "returnSecureToken": True})["idToken"]
    return uid, token


MEMBER = {"displayName": "Ada", "status": "active", "isAdmin": False, "profilePictureUrl": "https://x/p.png", "identityVerification": {"status": "verified"}}


@pytest.fixture
def live(db: Any, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(common, "verify_email", fake_verify_email)
    auth_mod.clear_profile_cache()
    return TestClient(create_app(init_db=False))


def test_real_member_token_reaches_their_own_data(live: TestClient, db: Any) -> None:
    uid, token = _user(MEMBER)
    r = live.post("/verify/single", headers={"Authorization": f"Bearer {token}"}, json={"email": "owner@shop.com"})
    assert r.status_code == 200, r.text
    assert db["verify_logs"].find_one()["user_id"] == uid


@pytest.mark.parametrize(("profile", "code", "detail"), [
    ({**MEMBER, "status": "suspended"}, 403, "Account suspended"),
    ({**MEMBER, "status": "banned"}, 403, "Account banned"),
    ({**MEMBER, "identityVerification": {"status": "pending"}}, 403, "Identity verification required on Elite"),
    (None, 403, "Elite profile not found"),
])
def test_real_tokens_with_elite_member_states(live: TestClient, profile: dict[str, Any] | None, code: int, detail: str) -> None:
    _, token = _user(profile)
    r = live.get("/dashboard/me", headers={"Authorization": f"Bearer {token}"})
    assert (r.status_code, r.json()["detail"]) == (code, detail)


def test_real_admin_token(live: TestClient) -> None:
    _, admin = _user({**MEMBER, "isAdmin": True})
    _, member = _user(MEMBER)
    assert live.get("/admin/leads/stats", headers={"Authorization": f"Bearer {admin}"}).status_code == 200
    assert live.get("/admin/leads/stats", headers={"Authorization": f"Bearer {member}"}).status_code == 403


def test_tampered_token_is_rejected(live: TestClient) -> None:
    _, token = _user(MEMBER)
    head, payload, sig = token.split(".")
    import base64

    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["aud"] = "some-other-project"
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    assert live.get("/dashboard/me", headers={"Authorization": f"Bearer {head}.{forged}.{sig}"}).status_code == 401
    assert live.get("/dashboard/me").status_code == 401
