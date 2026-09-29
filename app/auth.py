"""Elite identity for Email Scouting.

    Elite Firebase user → Firebase ID token → verify_id_token (Admin SDK)
    → Firebase UID → users/{uid} in the Elite Firestore → access decision

There is no Email Scouting account, password, JWT or signup. The UID comes
only from a verified token; a UID sent by the client (header, query, body)
is never read. Access rules mirror the Elite frontend gate:

    status "banned"                              → 403
    status "suspended"                           → 403
    no photo, or identityVerification.status
      not in {verified, manual_review}           → 403 (members only)
    isAdmin                                      → allowed; admin endpoints

Admin is Elite's users/{uid}.isAdmin — the same check the Elite VPS API uses.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cachetools import TTLCache
from fastapi import Depends, Header, HTTPException, status
from firebase_admin import auth as firebase_auth
from firebase_admin import exceptions as firebase_exceptions

from app.config import settings
from app.firebase import get_app, get_firestore

log = logging.getLogger("email_scouting.auth")

IDENTITY_PASSING = {"verified", "manual_review"}


@dataclass(frozen=True)
class Member:
    uid: str
    email: str
    name: str
    is_admin: bool
    status: str
    identity_status: str
    has_photo: bool

    @property
    def identity_passes(self) -> bool:
        return self.identity_status in IDENTITY_PASSING and self.has_photo


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, detail=detail)


def _unavailable() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Authentication is temporarily unavailable")


def bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise _unauthorized("Not authenticated")
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        raise _unauthorized("Not authenticated")
    return token


def verify_token(token: str) -> dict[str, Any]:
    """Verify signature, issuer, audience (Elite project) and expiry."""
    try:
        decoded: dict[str, Any] = firebase_auth.verify_id_token(token, app=get_app(), check_revoked=settings.check_revoked)
    except firebase_auth.ExpiredIdTokenError:
        raise _unauthorized("Token expired")
    except firebase_auth.RevokedIdTokenError:
        raise _unauthorized("Token revoked")
    except firebase_auth.UserDisabledError:
        raise _forbidden("Account disabled")
    except firebase_auth.CertificateFetchError:
        raise _unavailable()
    except (firebase_auth.InvalidIdTokenError, ValueError):
        raise _unauthorized("Invalid token")
    except firebase_exceptions.FirebaseError:
        # e.g. the revocation lookup couldn't reach Firebase: fail closed.
        raise _unavailable()
    uid = decoded.get("uid") or decoded.get("sub")
    if not isinstance(uid, str) or not uid:
        raise _unauthorized("Invalid token")
    return decoded


def _read_profile(uid: str) -> dict[str, Any] | None:
    snap = get_firestore().collection("users").document(uid).get()
    return snap.to_dict() if snap.exists else None


# Short cache so every request doesn't hit Firestore; status/admin changes
# on Elite take effect within PROFILE_CACHE_SECONDS.
_profile_cache: TTLCache[str, dict[str, Any] | None] = TTLCache(maxsize=10_000, ttl=max(settings.profile_cache_seconds, 1))
profile_loader: Callable[[str], dict[str, Any] | None] = _read_profile


def load_profile(uid: str) -> dict[str, Any] | None:
    if settings.profile_cache_seconds > 0 and uid in _profile_cache:
        return _profile_cache[uid]
    profile = profile_loader(uid)
    # A missing profile isn't cached, so a member who just signed up on Elite
    # isn't locked out for the cache window. Read errors propagate (never cached).
    if profile is not None and settings.profile_cache_seconds > 0:
        _profile_cache[uid] = profile
    return profile


def clear_profile_cache() -> None:
    _profile_cache.clear()


def member_from(uid: str, decoded: dict[str, Any], profile: dict[str, Any]) -> Member:
    idv = profile.get("identityVerification") or {}
    return Member(
        uid=uid,
        email=str(profile.get("email") or decoded.get("email") or ""),
        name=str(profile.get("displayName") or decoded.get("name") or ""),
        is_admin=profile.get("isAdmin") is True,
        status=str(profile.get("status") or "active"),
        identity_status=str(idv.get("status") or "pending") if isinstance(idv, dict) else "pending",
        has_photo=isinstance(profile.get("profilePictureUrl"), str) and bool(profile.get("profilePictureUrl")),
    )


def authorize(member: Member, *, need_identity: bool = True, need_admin: bool = False) -> Member:
    """Pure policy — shared by every dependency and unit-tested directly."""
    if need_admin:
        if not member.is_admin:
            raise _forbidden("Admin access required")
        return member
    if member.is_admin:
        return member  # Elite admins are never locked out (same as the frontend).
    if member.status == "banned":
        raise _forbidden("Account banned")
    if member.status == "suspended":
        raise _forbidden("Account suspended")
    if need_identity and not member.identity_passes:
        raise _forbidden("Identity verification required on Elite")
    return member


def get_current_member(authorization: str | None = Header(default=None)) -> Member:
    """Authenticated Elite member (token verified, profile loaded, not banned/suspended)."""
    decoded = verify_token(bearer_token(authorization))
    uid = str(decoded.get("uid") or decoded.get("sub"))
    try:
        profile = load_profile(uid)
    except Exception:
        # Firestore unreachable: fail closed with 503, never grant access.
        log.exception("Elite profile lookup failed")
        raise _unavailable()
    if profile is None:
        raise _forbidden("Elite profile not found")
    return authorize(member_from(uid, decoded, profile), need_identity=False)


def require_member(member: Member = Depends(get_current_member)) -> Member:
    """Approved member: the equivalent of MailClean's require_approved."""
    return authorize(member, need_identity=True)


def require_admin(member: Member = Depends(get_current_member)) -> Member:
    return authorize(member, need_admin=True)
