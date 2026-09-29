"""Test fixtures.

Persistence runs against the real Firestore emulator (wiped before each
test), started by `scripts/test.sh` / CI via `firebase emulators:exec`.
Endpoint tests use fake Elite profiles and a controllable token verifier;
real Firebase token verification is exercised in test_tokens.py and
test_emulator_integration.py."""

from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("PROFILE_CACHE_SECONDS", "0")
os.environ.setdefault("FIREBASE_PROJECT_ID", "demo-elite")

import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as firebase_auth

import app.auth as auth_mod
import app.common as common
from app import store
from app.main import create_app

PHOTO = "https://example.com/p.png"
VERIFIED = {"status": "verified"}

PROFILES: dict[str, dict[str, Any]] = {
    "uid-member": {"displayName": "Ada Obi", "email": "ada@elite.test", "status": "active", "isAdmin": False, "profilePictureUrl": PHOTO, "identityVerification": VERIFIED},
    "uid-member-2": {"displayName": "Bola", "email": "bola@elite.test", "status": "active", "isAdmin": False, "profilePictureUrl": PHOTO, "identityVerification": {"status": "manual_review"}},
    "uid-admin": {"displayName": "Tunde", "email": "tunde@elite.test", "status": "active", "isAdmin": True, "profilePictureUrl": None, "identityVerification": {"status": "pending"}},
    "uid-suspended": {"displayName": "Sus", "email": "s@elite.test", "status": "suspended", "isAdmin": False, "profilePictureUrl": PHOTO, "identityVerification": VERIFIED},
    "uid-banned": {"displayName": "Ban", "email": "b@elite.test", "status": "banned", "isAdmin": False, "profilePictureUrl": PHOTO, "identityVerification": VERIFIED},
    "uid-unverified": {"displayName": "New", "email": "n@elite.test", "status": "active", "isAdmin": False, "profilePictureUrl": PHOTO, "identityVerification": {"status": "pending"}},
    "uid-nophoto": {"displayName": "NoPic", "email": "np@elite.test", "status": "active", "isAdmin": False, "profilePictureUrl": None, "identityVerification": VERIFIED},
    "uid-suspended-admin": {"displayName": "SA", "email": "sa@elite.test", "status": "suspended", "isAdmin": True},
}

# token → uid. Anything else is rejected the way the Admin SDK would.
TOKENS = {f"tok-{uid.removeprefix('uid-')}": uid for uid in [*PROFILES, "uid-noprofile"]}


def fake_verify_id_token(token: str, app: Any = None, check_revoked: bool = False) -> dict[str, Any]:
    if token == "tok-expired":
        raise firebase_auth.ExpiredIdTokenError("Token expired", None)
    if token == "tok-revoked":
        raise firebase_auth.RevokedIdTokenError("revoked")
    if token == "tok-disabled":
        raise firebase_auth.UserDisabledError("disabled")
    if token not in TOKENS:
        raise firebase_auth.InvalidIdTokenError("bad token")
    uid = TOKENS[token]
    return {"uid": uid, "sub": uid, "email": f"{uid}@token.test"}


def fake_verify_email(email: str) -> dict[str, Any]:
    e = email.strip().lower()
    base = {"email": e, "format_valid": True, "mx_valid": True, "smtp_valid": True, "catch_all": False, "disposable": False}
    if "bad" in e:
        return {**base, "mx_valid": False, "smtp_valid": None, "risk": "HIGH", "category": "bounce", "sendable": False, "message": "No MX records"}
    if "catch" in e:
        return {**base, "catch_all": True, "risk": "MEDIUM", "category": "unknown", "sendable": True, "message": "Mailbox confirmed"}
    return {**base, "risk": "LOW", "category": "delivers", "sendable": True, "message": "Mailbox confirmed"}


FIRESTORE = os.getenv("FIRESTORE_EMULATOR_HOST")


def reset_firestore() -> None:
    project = os.environ["FIREBASE_PROJECT_ID"]
    req = urllib.request.Request(f"http://{FIRESTORE}/emulator/v1/projects/{project}/databases/(default)/documents", method="DELETE")
    urllib.request.urlopen(req).close()


@pytest.fixture
def db() -> Any:
    """A clean Firestore (emulator). Returns the Admin SDK client used by the app."""
    if not FIRESTORE:
        pytest.skip("Firestore emulator not running — use scripts/test.sh")
    reset_firestore()
    return store.fs()


def docs(db: Any, collection: str, **where: Any) -> list[dict[str, Any]]:
    q = db.collection(collection)
    for k, v in where.items():
        q = q.where(filter=store.FieldFilter(k, "==", v))
    return [s.to_dict() for s in q.stream()]


@pytest.fixture
def client(db: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(firebase_auth, "verify_id_token", fake_verify_id_token)
    monkeypatch.setattr(auth_mod, "get_app", lambda: None)
    monkeypatch.setattr(auth_mod, "profile_loader", lambda uid: PROFILES.get(uid))
    monkeypatch.setattr(common, "verify_email", fake_verify_email)
    auth_mod.clear_profile_cache()
    with TestClient(create_app()) as c:
        yield c


def bearer(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer tok-{name}"}
