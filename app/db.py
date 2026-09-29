"""MongoDB for Email Scouting *application data only*.

There is no users collection. Every per-member record stores
`user_id = <Firebase UID>`; `user_name`/`user_email` are display snapshots
for admin views, never used for identity.

Collections
    leads            lead pools (admin uploads), claimed by UID
    lead_jobs        claim-and-verify jobs (results embedded, ≤2000)
    verify_jobs      bulk CSV verification jobs (new: MailClean kept these in memory)
    verify_results   one row per bulk-verified email (avoids 16MB docs)
    verify_logs      verification counts for dashboards
    scout_logs       outreach batches logged as sent
    country_meta     display names for custom pools
"""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, MongoClient
from pymongo.database import Database

from app.config import settings

_client: MongoClient[dict[str, Any]] | None = None
_db: Database[dict[str, Any]] | None = None


def get_db() -> Database[dict[str, Any]]:
    global _client, _db
    if _db is None:
        if not settings.mongo_uri:
            raise RuntimeError("MONGO_URI is not set")
        _client = MongoClient(settings.mongo_uri, tz_aware=False)
        _db = _client[settings.mongo_db]
    return _db


def set_db(db: Database[dict[str, Any]]) -> None:
    """Inject a database (tests)."""
    global _db
    _db = db


def ensure_indexes(db: Database[dict[str, Any]] | None = None) -> None:
    d = db if db is not None else get_db()
    d["leads"].create_index([("email", ASCENDING), ("country", ASCENDING)], unique=True)
    d["leads"].create_index("status")
    d["leads"].create_index("country")
    d["leads"].create_index([("claimed_by", ASCENDING), ("claimed_at", ASCENDING)])
    d["lead_jobs"].create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
    d["lead_jobs"].create_index("status")
    d["verify_jobs"].create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
    d["verify_results"].create_index([("job_id", ASCENDING), ("idx", ASCENDING)])
    d["verify_logs"].create_index([("user_id", ASCENDING), ("sent_at", ASCENDING)])
    d["scout_logs"].create_index([("user_id", ASCENDING), ("sent_at", ASCENDING)])
    d["country_meta"].create_index("code", unique=True)
