"""Firebase authentication and Elite permission policy on real endpoints."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth import Member, authorize, bearer_token
from app.main import create_app
from tests.conftest import bearer

MEMBER_ENDPOINT = "/dashboard/me"
ADMIN_ENDPOINT = "/admin/leads/stats"


# ── unauthenticated / malformed ───────────────────────────────────────────────

def test_unauthenticated_request_is_401(client: TestClient) -> None:
    r = client.get(MEMBER_ENDPOINT)
    assert r.status_code == 401
    assert r.json()["detail"] == "Not authenticated"
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer ", "Basic abc", "Token tok-member", "Bearer a b", "tok-member"])
def test_malformed_authorization_header_is_401(client: TestClient, header: str) -> None:
    assert client.get(MEMBER_ENDPOINT, headers={"Authorization": header}).status_code == 401


def test_invalid_firebase_token_is_401(client: TestClient) -> None:
    r = client.get(MEMBER_ENDPOINT, headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid token"


def test_expired_firebase_token_is_401(client: TestClient) -> None:
    r = client.get(MEMBER_ENDPOINT, headers=bearer("expired"))
    assert r.status_code == 401
    assert r.json()["detail"] == "Token expired"


def test_revoked_firebase_token_is_401(client: TestClient) -> None:
    assert client.get(MEMBER_ENDPOINT, headers=bearer("revoked")).json()["detail"] == "Token revoked"


def test_disabled_firebase_user_is_403(client: TestClient) -> None:
    assert client.get(MEMBER_ENDPOINT, headers=bearer("disabled")).status_code == 403


def test_valid_token_without_elite_profile_is_403(client: TestClient) -> None:
    r = client.get(MEMBER_ENDPOINT, headers=bearer("noprofile"))
    assert r.status_code == 403
    assert r.json()["detail"] == "Elite profile not found"


# ── valid token / UID ─────────────────────────────────────────────────────────

def test_valid_token_normal_member_is_allowed(client: TestClient) -> None:
    r = client.get(MEMBER_ENDPOINT, headers=bearer("member"))
    assert r.status_code == 200
    assert set(r.json()) >= {"verified", "scouted", "verify_series", "scout_series", "recent_activity"}


def test_manual_review_member_is_allowed(client: TestClient) -> None:
    assert client.get(MEMBER_ENDPOINT, headers=bearer("member-2")).status_code == 200


def test_uid_comes_from_the_token_not_the_client(client: TestClient, db) -> None:  # type: ignore[no-untyped-def]
    """A UID sent by the client (header, query, body) is ignored."""
    r = client.post(
        "/scout/log?uid=uid-member-2&user_id=uid-member-2",
        headers={**bearer("member"), "X-User-Id": "uid-member-2", "X-Firebase-Uid": "uid-member-2"},
        json={"batch_number": 1, "total_batches": 1, "email_count": 10, "subject": "Hi", "user_id": "uid-member-2", "uid": "uid-member-2"},
    )
    assert r.status_code == 200
    rows = list(db["scout_logs"].find())
    assert len(rows) == 1
    assert rows[0]["user_id"] == "uid-member"


# ── member status ─────────────────────────────────────────────────────────────

def test_suspended_member_is_403(client: TestClient) -> None:
    r = client.get(MEMBER_ENDPOINT, headers=bearer("suspended"))
    assert r.status_code == 403
    assert r.json()["detail"] == "Account suspended"


def test_banned_member_is_403(client: TestClient) -> None:
    r = client.get(MEMBER_ENDPOINT, headers=bearer("banned"))
    assert r.status_code == 403
    assert r.json()["detail"] == "Account banned"


@pytest.mark.parametrize("who", ["unverified", "nophoto"])
def test_unverified_member_is_403_like_the_frontend_gate(client: TestClient, who: str) -> None:
    r = client.get(MEMBER_ENDPOINT, headers=bearer(who))
    assert r.status_code == 403
    assert "Identity verification" in r.json()["detail"]


# ── admin ─────────────────────────────────────────────────────────────────────

def test_admin_can_use_admin_endpoints(client: TestClient) -> None:
    assert client.get(ADMIN_ENDPOINT, headers=bearer("admin")).status_code == 200


def test_admin_is_never_locked_out_of_member_endpoints(client: TestClient) -> None:
    # Unverified + no photo, but Elite admin (same exemption as the frontend).
    assert client.get(MEMBER_ENDPOINT, headers=bearer("admin")).status_code == 200
    assert client.get(MEMBER_ENDPOINT, headers=bearer("suspended-admin")).status_code == 200


@pytest.mark.parametrize("who", ["member", "member-2", "unverified"])
def test_non_admins_get_403_on_admin_endpoints(client: TestClient, who: str) -> None:
    r = client.get(ADMIN_ENDPOINT, headers=bearer(who))
    assert r.status_code == 403
    assert r.json()["detail"] == "Admin access required"


# ── every protected endpoint ─────────────────────────────────────────────────

def _protected_routes() -> list[tuple[str, str]]:
    out = []
    for route in create_app(init_db=False).routes:
        if isinstance(route, APIRoute) and route.path != "/health":
            for method in route.methods - {"HEAD", "OPTIONS"}:
                out.append((method, route.path.replace("{job_id}", "0" * 24).replace("{country}", "US").replace("{uid}", "uid-member")))
    return sorted(out)


def test_route_inventory_is_what_the_frontend_needs() -> None:
    paths = {p for _, p in _protected_routes()}
    for needed in ["/dashboard/me", "/verify/single", "/verify/bulk", "/leads/countries", "/leads/claim-and-verify", "/leads/history", "/scout/log", "/admin/leads/stats"]:
        assert needed in paths


@pytest.mark.parametrize(("method", "path"), _protected_routes())
def test_every_protected_endpoint_rejects_missing_token(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize(("method", "path"), _protected_routes())
def test_every_protected_endpoint_rejects_expired_token(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path, headers=bearer("expired")).status_code == 401


@pytest.mark.parametrize(("method", "path"), _protected_routes())
def test_every_protected_endpoint_rejects_banned_member(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path, headers=bearer("banned")).status_code == 403


def test_health_is_public(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/auth/signup"), ("POST", "/auth/login"), ("GET", "/auth/me"), ("GET", "/admin/users"), ("PUT", "/admin/users/x/status"),
     ("GET", "/auth/google/connect"), ("POST", "/gmail/send-batch"), ("GET", "/gmail/accounts"), ("GET", "/pixel/abc"), ("POST", "/scout/create-track")],
)
def test_old_mailclean_auth_and_gmail_routes_are_gone(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code in (404, 405)


# ── pure policy ───────────────────────────────────────────────────────────────

def _m(**kw: object) -> Member:
    base: dict[str, object] = {"uid": "u", "email": "", "name": "", "is_admin": False, "status": "active", "identity_status": "verified", "has_photo": True}
    base.update(kw)
    return Member(**base)  # type: ignore[arg-type]


def test_policy_matrix() -> None:
    from fastapi import HTTPException

    assert authorize(_m()).uid == "u"
    for kw, detail in [({"status": "banned"}, "Account banned"), ({"status": "suspended"}, "Account suspended"), ({"identity_status": "failed"}, "Identity verification required on Elite")]:
        with pytest.raises(HTTPException) as e:
            authorize(_m(**kw))
        assert e.value.status_code == 403 and e.value.detail == detail
    assert authorize(_m(identity_status="pending"), need_identity=False).uid == "u"
    with pytest.raises(HTTPException):
        authorize(_m(), need_admin=True)
    assert authorize(_m(is_admin=True, status="banned"), need_admin=True).is_admin


def test_bearer_parsing() -> None:
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer   abc  ") == "abc"
