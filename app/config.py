"""Runtime configuration. Every value comes from the environment; secrets are
never hardcoded and never shipped to the frontend."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _list(name: str, default: str) -> list[str]:
    return [v.strip().rstrip("/") for v in os.getenv(name, default).split(",") if v.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # "production" disables emulator shortcuts.
    env: str = field(default_factory=lambda: os.getenv("APP_ENV", "production"))

    # Elite Firebase project — the only identity provider.
    firebase_project_id: str = field(default_factory=lambda: os.getenv("FIREBASE_PROJECT_ID", "eliteachievers-b8782"))
    # Server-side path to the service-account JSON (same convention as the
    # Elite VPS API). Unset → Application Default Credentials.
    firebase_service_account_path: str = field(default_factory=lambda: os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", ""))
    # Reject tokens revoked via Firebase (sign-out-everywhere, password change).
    check_revoked: bool = field(default_factory=lambda: os.getenv("FIREBASE_CHECK_REVOKED", "true").lower() != "false")
    # How long a member's Elite profile (status/admin/identity) is cached.
    profile_cache_seconds: int = field(default_factory=lambda: _int("PROFILE_CACHE_SECONDS", 30))

    # Email Scouting application data (leads, jobs, logs) — no user accounts.
    mongo_uri: str = field(default_factory=lambda: os.getenv("MONGO_URI", ""))
    mongo_db: str = field(default_factory=lambda: os.getenv("MONGO_DB", "email_scouting"))

    allowed_origins: list[str] = field(default_factory=lambda: _list("ALLOWED_ORIGINS", "https://scout.eliteachieversteam.com"))

    # Verifier (unchanged MailClean settings).
    smtp_timeout: int = field(default_factory=lambda: _int("SMTP_TIMEOUT", 10))
    max_threads: int = field(default_factory=lambda: _int("MAX_THREADS", 30))
    from_email: str = field(default_factory=lambda: os.getenv("FROM_EMAIL", "verify@eliteachieversteam.com"))
    retry_count: int = field(default_factory=lambda: _int("RETRY_COUNT", 2))

    # Business limits (MailClean values).
    max_claim_per_day: int = field(default_factory=lambda: _int("MAX_CLAIM_PER_USER_PER_DAY", 3000))
    max_bulk_emails: int = field(default_factory=lambda: _int("MAX_BULK_EMAILS", 50_000))
    max_upload_bytes: int = field(default_factory=lambda: _int("MAX_UPLOAD_BYTES", 20 * 1024 * 1024))

    @property
    def is_production(self) -> bool:
        return self.env == "production"


settings = Settings()
