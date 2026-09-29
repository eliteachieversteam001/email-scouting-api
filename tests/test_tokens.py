"""Real Firebase Admin SDK verification (no mocked verify_id_token).

Tokens are RS256-signed with a local key; the SDK's public-cert fetch is
pointed at that key, so signature, audience (the Elite project), issuer,
expiry and subject are all checked by firebase_admin/google-auth exactly
as in production.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import time
from collections.abc import Iterator
from typing import Any

import firebase_admin
import google.auth.crypt
import google.auth.jwt
import google.oauth2.id_token
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.auth as auth_mod
import app.common as common
from app.firebase import _EmulatorCredential
from app.main import create_app
from tests.conftest import PROFILES, fake_verify_email

PROJECT = "eliteachievers-b8782"
KID = "test-kid-1"


def _keypair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "securetoken-test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1)).sign(key, hashes.SHA256())
    )
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return pem_key, cert.public_bytes(serialization.Encoding.PEM)


SIGNING_KEY, CERT = _keypair()
OTHER_KEY, _ = _keypair()


def make_token(uid: str = "uid-member", *, project: str = PROJECT, exp_offset: int = 3600, key: bytes = SIGNING_KEY, kid: str | None = KID, **extra: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://securetoken.google.com/{project}", "aud": project, "auth_time": now - 60,
        "sub": uid, "user_id": uid, "iat": now - 60, "exp": now + exp_offset, "email": f"{uid}@elite.test",
        "firebase": {"sign_in_provider": "password"}, **extra,
    }
    signer = google.auth.crypt.RSASigner.from_string(key, key_id=kid)
    return google.auth.jwt.encode(signer, claims).decode()


@pytest.fixture
def real_firebase(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("FIREBASE_AUTH_EMULATOR_HOST", raising=False)
    fb_app = firebase_admin.initialize_app(_EmulatorCredential(), {"projectId": PROJECT}, name=f"tokens-{time.time_ns()}")
    monkeypatch.setattr(auth_mod, "get_app", lambda: fb_app)
    monkeypatch.setattr(auth_mod, "settings", dataclasses.replace(auth_mod.settings, check_revoked=False))
    monkeypatch.setattr(google.oauth2.id_token, "_fetch_certs", lambda request, url: {KID: CERT.decode()})
    yield
    firebase_admin.delete_app(fb_app)


def test_valid_token_is_verified_and_uid_extracted(real_firebase: None) -> None:
    decoded = auth_mod.verify_token(make_token("uid-abc123"))
    assert decoded["uid"] == "uid-abc123"
    assert decoded["aud"] == PROJECT


@pytest.mark.parametrize(
    ("token_kwargs", "detail"),
    [
        ({"exp_offset": -120}, "Token expired"),
        ({"project": "some-other-project"}, "Invalid token"),  # token from a different Firebase project
        ({"key": OTHER_KEY}, "Invalid token"),  # forged signature
        ({"kid": None}, "Invalid token"),
        ({"uid": ""}, "Invalid token"),
    ],
)
def test_bad_tokens_are_rejected_with_401(real_firebase: None, token_kwargs: dict[str, Any], detail: str) -> None:
    with pytest.raises(HTTPException) as e:
        auth_mod.verify_token(make_token(**token_kwargs))
    assert e.value.status_code == 401
    assert e.value.detail == detail


@pytest.mark.parametrize("garbage", ["abc", "a.b.c", "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0."])
def test_garbage_tokens_are_401(real_firebase: None, garbage: str) -> None:
    with pytest.raises(HTTPException) as e:
        auth_mod.verify_token(garbage)
    assert e.value.status_code == 401


def test_protected_endpoint_end_to_end_with_real_verification(real_firebase: None, db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_mod, "profile_loader", lambda uid: PROFILES.get(uid))
    monkeypatch.setattr(common, "verify_email", fake_verify_email)
    with TestClient(create_app(init_db=False)) as c:
        ok = c.post("/verify/single", headers={"Authorization": f"Bearer {make_token('uid-member')}"}, json={"email": "owner@shop.com"})
        assert ok.status_code == 200
        assert ok.json()["category"] == "delivers"
        assert db["verify_logs"].find_one()["user_id"] == "uid-member"

        assert c.get("/dashboard/me", headers={"Authorization": f"Bearer {make_token(exp_offset=-60)}"}).status_code == 401
        assert c.get("/dashboard/me", headers={"Authorization": f"Bearer {make_token(project='evil')}"}).status_code == 401
        assert c.get("/dashboard/me", headers={"Authorization": f"Bearer {make_token('uid-banned')}"}).status_code == 403
        assert c.get("/admin/leads/stats", headers={"Authorization": f"Bearer {make_token('uid-member')}"}).status_code == 403
        assert c.get("/admin/leads/stats", headers={"Authorization": f"Bearer {make_token('uid-admin')}"}).status_code == 200
