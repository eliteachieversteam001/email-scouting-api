"""Firestore persistence for Email Scouting (Elite project, Admin SDK).

Every per-member document carries the Firebase UID from the *verified* ID
token (`userId` / `claimedBy`). Collections (see docs/MIGRATION.md):

    scouting_pools/{CODE}
    scouting_leads/{sha1(CODE:email)}
    scouting_jobs/{id}                 + results/{000001…}
    email_verifications/{id}           + results/{000001…}
    scouting_history/{id}              type: verification | outreach
    scouting_usage/{uid}               + days/{YYYY-MM-DD}   (and _platform)

These collections are server-only: Elite's security rules don't open them
to browser clients, and only this API (Admin SDK) reads or writes them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud import firestore as gcf
from google.cloud.firestore_v1.base_query import FieldFilter

from app.firebase import get_firestore

POOLS = "scouting_pools"
LEADS = "scouting_leads"
JOBS = "scouting_jobs"
VERIFICATIONS = "email_verifications"
HISTORY = "scouting_history"
USAGE = "scouting_usage"
RESULTS = "results"
DAYS = "days"
PLATFORM = "_platform"  # reserved usage doc for platform-wide totals

BATCH_LIMIT = 400  # Firestore allows 500 writes per batch; stay under it


def fs() -> Any:
    return get_firestore()


def now() -> datetime:
    return datetime.now(UTC)


def _eq(field: str, value: Any) -> FieldFilter:
    return FieldFilter(field, "==", value)


def count(query: Any) -> int:
    return int(query.count().get()[0][0].value)


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def with_id(snap: Any) -> dict[str, Any]:
    data: dict[str, Any] = snap.to_dict() or {}
    data["id"] = snap.id
    return data


# ── Pools & leads ─────────────────────────────────────────────────────────────

def lead_id(code: str, email: str) -> str:
    return hashlib.sha1(f"{code}:{email}".encode(), usedforsecurity=False).hexdigest()


def get_pool(code: str) -> dict[str, Any] | None:
    snap = fs().collection(POOLS).document(code).get()
    return snap.to_dict() if snap.exists else None


def upsert_pool(code: str, name: str | None, uid: str) -> None:
    ref = fs().collection(POOLS).document(code)
    data: dict[str, Any] = {"code": code, "updatedAt": now()}
    if name:
        data["name"] = name
    if not ref.get().exists:
        data.update({"createdBy": uid, "createdAt": now(), "flag": data.get("flag", "")})
    ref.set(data, merge=True)


def pool_codes() -> list[str]:
    return sorted(s.id for s in fs().collection(POOLS).stream())


def _leads_in(code: str) -> Any:
    return fs().collection(LEADS).where(filter=_eq("country", code))


def pool_counts(code: str) -> dict[str, int]:
    q = _leads_in(code)
    return {
        "total": count(q),
        "available": count(q.where(filter=_eq("status", "available"))),
        "claimed": count(q.where(filter=_eq("status", "claimed"))),
        "expired": count(q.where(filter=_eq("status", "expired"))),
    }


def available_count(code: str) -> int:
    return count(_leads_in(code).where(filter=_eq("status", "available")))


def insert_leads(code: str, rows: Iterable[tuple[str, str]], uid: str) -> tuple[int, int]:
    """Insert (email, domain) rows into a pool; duplicates (same pool+email) are skipped."""
    db = fs()
    col = db.collection(LEADS)
    unique: dict[str, tuple[str, str]] = {}
    skipped = 0
    for email, domain in rows:
        key = lead_id(code, email)
        if key in unique:
            skipped += 1
        else:
            unique[key] = (email, domain)
    inserted = 0
    stamp = now()
    for keys in _chunks(list(unique), BATCH_LIMIT):
        refs = [col.document(k) for k in keys]
        existing = {s.id for s in db.get_all(refs) if s.exists}
        batch = db.batch()
        for ref in refs:
            if ref.id in existing:
                skipped += 1
                continue
            email, domain = unique[ref.id]
            batch.create(ref, {
                "email": email, "country": code, "domain": domain, "status": "available",
                "claimedBy": None, "claimedByJob": None, "claimedAt": None,
                "verifiedResult": None, "verifiedAt": None, "uploadedBy": uid, "uploadedAt": stamp,
            })
            inserted += 1
        batch.commit()
    return inserted, skipped


def delete_leads(code: str, available_only: bool) -> int:
    q = _leads_in(code)
    if available_only:
        q = q.where(filter=_eq("status", "available"))
    deleted = 0
    while True:
        snaps = list(q.limit(BATCH_LIMIT).stream())
        if not snaps:
            return deleted
        batch = fs().batch()
        for s in snaps:
            batch.delete(s.reference)
        batch.commit()
        deleted += len(snaps)


def sample_leads(code: str, limit: int = 10) -> list[dict[str, Any]]:
    return [{"email": d.get("email"), "domain": d.get("domain", ""), "status": d.get("status")} for d in (s.to_dict() for s in _leads_in(code).limit(limit).stream())]


def claim_leads(code: str, amount: int, uid: str, job_id: str) -> list[str]:
    """Atomically move up to `amount` available leads to this member/job.

    Each chunk is claimed in a transaction that re-reads the leads, so two
    members can never claim the same lead (MailClean used find_one_and_update).
    """
    db = fs()
    claimed: list[str] = []
    stalls = 0
    while len(claimed) < amount and stalls < 5:
        want = min(amount - len(claimed), 200)
        candidates = [s.reference for s in _leads_in(code).where(filter=_eq("status", "available")).limit(want).stream()]
        if not candidates:
            break

        @gcf.transactional
        def claim(txn: Any, refs: list[Any]) -> list[str]:
            got: list[str] = []
            stamp = now()
            for snap in db.get_all(refs, transaction=txn):
                data = snap.to_dict() or {}
                if snap.exists and data.get("status") == "available":
                    txn.update(snap.reference, {"status": "claimed", "claimedBy": uid, "claimedByJob": job_id, "claimedAt": stamp})
                    got.append(data["email"])
            return got

        got = claim(db.transaction(), candidates)
        claimed.extend(got)
        stalls = 0 if got else stalls + 1
    return claimed


def mark_lead_verified(code: str, email: str, job_id: str, result: dict[str, Any]) -> None:
    ref = fs().collection(LEADS).document(lead_id(code, email))
    snap = ref.get()
    if snap.exists and (snap.to_dict() or {}).get("claimedByJob") == job_id:
        ref.update({"verifiedResult": result, "verifiedAt": now()})


def release_unverified_leads(job_id: str) -> None:
    q = fs().collection(LEADS).where(filter=_eq("claimedByJob", job_id)).where(filter=_eq("verifiedResult", None))
    snaps = list(q.stream())
    for chunk in _chunks(snaps, BATCH_LIMIT):
        batch = fs().batch()
        for s in chunk:
            batch.update(s.reference, {"status": "available", "claimedBy": None, "claimedByJob": None, "claimedAt": None})
        batch.commit()


# ── Jobs (scouting_jobs, email_verifications) ────────────────────────────────

def create_doc(collection: str, data: dict[str, Any]) -> str:
    ref = fs().collection(collection).document()
    ref.set(data)
    return str(ref.id)


def get_doc(collection: str, doc_id: str) -> dict[str, Any] | None:
    snap = fs().collection(collection).document(doc_id).get()
    return with_id(snap) if snap.exists else None


def update_doc(collection: str, doc_id: str, fields: dict[str, Any]) -> None:
    fs().collection(collection).document(doc_id).update(fields)


def write_results(collection: str, doc_id: str, start_idx: int, results: list[dict[str, Any]]) -> None:
    col = fs().collection(collection).document(doc_id).collection(RESULTS)
    for offset, chunk in enumerate(_chunks(results, BATCH_LIMIT)):
        batch = fs().batch()
        for i, r in enumerate(chunk):
            idx = start_idx + offset * BATCH_LIMIT + i
            batch.set(col.document(f"{idx:06d}"), {"idx": idx, "result": r})
        batch.commit()


def read_results(collection: str, doc_id: str) -> list[dict[str, Any]]:
    col = fs().collection(collection).document(doc_id).collection(RESULTS)
    return [(s.to_dict() or {}).get("result", {}) for s in col.order_by("idx").stream()]


def user_has_active_job(uid: str) -> bool:
    q = fs().collection(JOBS).where(filter=_eq("userId", uid)).where(filter=_eq("status", "processing")).limit(1)
    return any(True for _ in q.stream())


def user_jobs(uid: str, limit: int = 20) -> list[dict[str, Any]]:
    q = fs().collection(JOBS).where(filter=_eq("userId", uid)).order_by("createdAt", direction=gcf.Query.DESCENDING).limit(limit)
    return [with_id(s) for s in q.stream()]


def all_jobs(limit: int = 100) -> list[dict[str, Any]]:
    q = fs().collection(JOBS).order_by("createdAt", direction=gcf.Query.DESCENDING).limit(limit)
    return [with_id(s) for s in q.stream()]


def abandoned_jobs(older_than: timedelta) -> list[dict[str, Any]]:
    q = fs().collection(JOBS).where(filter=_eq("status", "processing")).where(filter=FieldFilter("createdAt", "<", now() - older_than))
    return [with_id(s) for s in q.stream()]


# ── History & usage ───────────────────────────────────────────────────────────

def _day(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def _bump_usage(uid: str, fields: dict[str, int]) -> None:
    """Atomic increments on the member's and the platform's totals + today's day doc."""
    db = fs()
    batch = db.batch()
    stamp = now()
    inc = {k: gcf.Increment(v) for k, v in fields.items()}
    for owner in (uid, PLATFORM):
        root = db.collection(USAGE).document(owner)
        batch.set(root, {**inc, "userId": owner, "updatedAt": stamp}, merge=True)
        batch.set(root.collection(DAYS).document(_day(stamp)), {**inc, "date": _day(stamp)}, merge=True)
    batch.commit()


def record_verification(uid: str, name: str, email_count: int, source: str) -> None:
    fs().collection(HISTORY).add({"type": "verification", "userId": uid, "userName": name, "source": source, "emailCount": email_count, "createdAt": now()})
    _bump_usage(uid, {"verified": email_count})


def record_outreach(uid: str, name: str, batch_number: int, total_batches: int, email_count: int, subject: str) -> None:
    fs().collection(HISTORY).add({
        "type": "outreach", "userId": uid, "userName": name, "batchNumber": batch_number,
        "totalBatches": total_batches, "emailCount": email_count, "subject": subject, "createdAt": now(),
    })
    _bump_usage(uid, {"scouted": email_count, "batches": 1})


def record_claim(uid: str, n: int) -> None:
    _bump_usage(uid, {"claimed": n})


def claimed_today(uid: str) -> int:
    snap = fs().collection(USAGE).document(uid).collection(DAYS).document(_day(now())).get()
    return int((snap.to_dict() or {}).get("claimed", 0)) if snap.exists else 0


def usage(owner: str, days: int = 30) -> dict[str, Any]:
    """Totals + today/week/month + a `days`-long daily series (from day docs)."""
    current = now()
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=current.weekday())
    month = today.replace(day=1)
    series_start = today - timedelta(days=days - 1)
    since = min(month, series_start)

    root = fs().collection(USAGE).document(owner)
    root_snap = root.get()
    totals = root_snap.to_dict() or {} if root_snap.exists else {}
    by_day = {s.id: s.to_dict() or {} for s in root.collection(DAYS).where(filter=FieldFilter("date", ">=", _day(since))).stream()}

    def total_since(field: str, start: datetime) -> int:
        return sum(int(v.get(field, 0)) for k, v in by_day.items() if k >= _day(start))

    def series(field: str) -> list[dict[str, Any]]:
        out = []
        for i in range(days):
            d = series_start + timedelta(days=i)
            out.append({"date": d.strftime("%b %d"), "value": int(by_day.get(_day(d), {}).get(field, 0))})
        return out

    def periods(field: str) -> dict[str, int]:
        return {"today": total_since(field, today), "week": total_since(field, week), "month": total_since(field, month), "total": int(totals.get(field, 0))}

    return {
        "verified": periods("verified"),
        "scouted": periods("scouted"),
        "batches": periods("batches"),
        "verify_series": series("verified"),
        "scout_series": series("scouted"),
    }


def recent_outreach(uid: str | None, limit: int) -> list[dict[str, Any]]:
    q = fs().collection(HISTORY).where(filter=_eq("type", "outreach"))
    if uid:
        q = q.where(filter=_eq("userId", uid))
    q = q.order_by("createdAt", direction=gcf.Query.DESCENDING).limit(limit)
    return [s.to_dict() or {} for s in q.stream()]


def history_since(type_: str, since: datetime, limit: int = 5000) -> list[dict[str, Any]]:
    q = fs().collection(HISTORY).where(filter=_eq("type", type_)).where(filter=FieldFilter("createdAt", ">=", since)).limit(limit)
    return [s.to_dict() or {} for s in q.stream()]
