"""Dashboard statistics and outreach logging (MailClean routes_dashboard.py + /scout/log)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import Member, load_profile, require_admin, require_member
from app.common import iso, utcnow
from app.db import get_db

router = APIRouter()


def date_ranges() -> tuple[datetime, datetime, datetime]:
    now = utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=now.weekday())
    month = today.replace(day=1)
    return today, week, month


def _match(user_id: str | None, since: datetime | None) -> dict[str, Any]:
    q: dict[str, Any] = {}
    if user_id:
        q["user_id"] = user_id
    if since:
        q["sent_at"] = {"$gte": since}
    return q


def sum_field(collection: str, field: str, user_id: str | None = None, since: datetime | None = None) -> int:
    res = list(get_db()[collection].aggregate([{"$match": _match(user_id, since)}, {"$group": {"_id": None, "total": {"$sum": "$" + field}}}]))
    return int(res[0]["total"]) if res else 0


def count_docs(collection: str, user_id: str | None = None, since: datetime | None = None) -> int:
    return get_db()[collection].count_documents(_match(user_id, since))


def daily_series(collection: str, field: str, user_id: str | None = None, days: int = 30) -> list[dict[str, Any]]:
    now = utcnow()
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    q = _match(user_id, start)
    totals: dict[str, int] = {}
    for doc in get_db()[collection].find(q, {"sent_at": 1, field: 1}):
        key = doc["sent_at"].strftime("%Y-%m-%d")
        totals[key] = totals.get(key, 0) + int(doc.get(field) or 0)
    series = []
    for i in range(days):
        day = start + timedelta(days=i)
        series.append({"date": day.strftime("%b %d"), "value": totals.get(day.strftime("%Y-%m-%d"), 0)})
    return series


def _stats(uid: str | None) -> dict[str, Any]:
    today, week, month = date_ranges()
    verified = {k: sum_field("verify_logs", "email_count", uid, s) for k, s in (("today", today), ("week", week), ("month", month), ("total", None))}
    scouted: dict[str, Any] = {k: sum_field("scout_logs", "email_count", uid, s) for k, s in (("today", today), ("week", week), ("month", month), ("total", None))}
    if uid:
        for k, s in (("today", today), ("week", week), ("month", month), ("total", None)):
            scouted[f"batches_{k}"] = count_docs("scout_logs", uid, s)
    return {
        "verified": verified,
        "scouted": scouted,
        "verify_series": daily_series("verify_logs", "email_count", uid),
        "scout_series": daily_series("scout_logs", "email_count", uid),
    }


def _recent(uid: str | None, limit: int) -> list[dict[str, Any]]:
    q = {"user_id": uid} if uid else {}
    projection = {"_id": 0, "user_id": 0, "user_email": 0} if uid else {"_id": 0, "user_email": 0}
    rows = list(get_db()["scout_logs"].find(q, projection).sort("sent_at", -1).limit(limit))
    for r in rows:
        r["sent_at"] = iso(r.get("sent_at"))
    return rows


@router.get("/dashboard/me")
def my_dashboard(member: Member = Depends(require_member)) -> Any:
    return {**_stats(member.uid), "recent_activity": _recent(member.uid, 10)}


@router.get("/dashboard/admin")
def admin_dashboard(admin: Member = Depends(require_admin)) -> Any:
    _, _, month = date_ranges()
    top = list(get_db()["scout_logs"].aggregate([
        {"$match": {"sent_at": {"$gte": month}}},
        {"$group": {"_id": "$user_id", "name": {"$first": "$user_name"}, "total": {"$sum": "$email_count"}, "batches": {"$sum": 1}}},
        {"$sort": {"total": -1}}, {"$limit": 10},
    ]))
    for u in top:
        u["user_id"] = u.pop("_id")
    # Member counts live in Elite (Firestore), not here; report scouting activity only.
    active = len(get_db()["verify_logs"].distinct("user_id", {"sent_at": {"$gte": month}}))
    return {**_stats(None), "active_members_this_month": active, "top_users": top, "recent_activity": _recent(None, 20)}


@router.get("/dashboard/admin/user/{uid}")
def admin_user_detail(uid: str, admin: Member = Depends(require_admin)) -> Any:
    if not uid or len(uid) > 128:
        raise HTTPException(400, detail="Invalid user ID")
    profile = load_profile(uid)
    if profile is None:
        raise HTTPException(404, detail="User not found")
    stats = _stats(uid)
    verified_total = stats["verified"]["total"]
    return {
        "user": {
            "id": uid,
            "name": profile.get("displayName", ""),
            "email": profile.get("email", ""),
            "role": "admin" if profile.get("isAdmin") is True else "user",
            "status": profile.get("status", "active"),
        },
        **stats,
        "scout_rate": round((stats["scouted"]["total"] / verified_total) * 100) if verified_total > 0 else 0,
        "recent_scouts": _recent(uid, 20),
    }


class ScoutLogRequest(BaseModel):
    batch_number: int = Field(ge=1, le=100_000)
    email_count: int = Field(ge=0, le=10_000)
    subject: str = Field(max_length=300)
    total_batches: int = Field(ge=1, le=100_000)


@router.post("/scout/log")
def log_scout(payload: ScoutLogRequest, member: Member = Depends(require_member)) -> Any:
    get_db()["scout_logs"].insert_one({
        "user_id": member.uid, "user_name": member.name, "user_email": member.email,
        "batch_number": payload.batch_number, "email_count": payload.email_count,
        "subject": payload.subject, "total_batches": payload.total_batches, "sent_at": utcnow(),
    })
    return {"message": "Logged"}


@router.get("/admin/scout-logs")
def get_scout_logs(admin: Member = Depends(require_admin)) -> Any:
    return _recent(None, 200)
