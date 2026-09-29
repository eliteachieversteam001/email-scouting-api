"""Firebase Admin SDK for the Elite project (server-side only).

The service account lives on the server (FIREBASE_SERVICE_ACCOUNT_PATH or
Application Default Credentials). It is never sent to, or configured in,
the frontend.
"""

from __future__ import annotations

import os
import threading
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore

from app.config import settings

_lock = threading.Lock()
_app: firebase_admin.App | None = None


class _EmulatorCredential(credentials.Base):
    """Anonymous credential, only for the local Firebase emulators."""

    def get_credential(self) -> Any:
        from google.auth.credentials import AnonymousCredentials

        return AnonymousCredentials()


def using_emulators() -> bool:
    return bool(os.getenv("FIREBASE_AUTH_EMULATOR_HOST") or os.getenv("FIRESTORE_EMULATOR_HOST"))


def _credential() -> credentials.Base:
    if using_emulators():
        if settings.is_production:
            raise RuntimeError("Firebase emulator variables are set in production — refusing to start.")
        return _EmulatorCredential()
    if settings.firebase_service_account_path:
        return credentials.Certificate(settings.firebase_service_account_path)
    return credentials.ApplicationDefault()


def get_app() -> firebase_admin.App:
    global _app
    if _app is None:
        with _lock:
            if _app is None:
                _app = firebase_admin.initialize_app(
                    _credential(),
                    {"projectId": settings.firebase_project_id},
                    name="email-scouting",
                )
    return _app


def get_firestore() -> Any:
    return firestore.client(app=get_app())
