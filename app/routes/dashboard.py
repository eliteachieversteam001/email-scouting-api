"""Dashboard statistics and outreach logging (MailClean routes_dashboard.py + /scout/log).
Counts come from Firestore `scouting_usage` (atomic increments), activity from `scouting_history`."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import store
from app.auth import Member, load_profile, require_admin, require_member
from app.common import iso

router = APIRouter()


def _stats(owner: str, member_view: bool) -> dict[str, Any]:
    u = store.usage(owner)
    scouted: dict[str, Any] = dict(u["scouted"])
    if member_view:
        for k, v in u["batches"].items():
            scouted[f"batches_{k}"] = v
    return {"verified": u["verified"], "scouted": scouted, "verify_series": u["verify_series"], "scout_series": u["scout_series"]}


def _activity(rows: list[dict[str, Any]], admin: bool) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        item = {
            "batch_number": r.get("batchNumber", 0), "total_batches": r.get("totalBatches", 0),
            "email_count": r.get("emailCount", 0), "subject": r.get("subject", ""), "sent_at": iso(r.get("createdAt")),
        }
        if admin:
            item.update({"user_id": r.get("userId"), "user_name": r.get("userName", "")})
        out.append(item)
    return out


@router.get("/dashboard/me")
def my_dashboard(member: Member = Depends(require_member)) -> Any:
    return {**_stats(member.uid, True), "recent_activity": _activity(store.recent_outreach(member.uid, 10), admin=False)}


@router.get("/dashboard/admin")
def admin_dashboard(admin: Member = Depends(require_admin)) -> Any:
    month = store.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    top: dict[str, dict[str, Any]] = {}
    for r in store.history_since("outreach", month):
        t = top.setdefault(r.get("userId", ""), {"user_id": r.get("userId"), "name": r.get("userName", ""), "total": 0, "batches": 0})
        t["total"] += int(r.get("emailCount", 0))
        t["batches"] += 1
    active = {r.get("userId") for r in store.history_since("verification", month)}
    return {
        **_stats(store.PLATFORM, False),
        "active_members_this_month": len(active),
        "top_users": sorted(top.values(), key=lambda t: t["total"], reverse=True)[:10],
        "recent_activity": _activity(store.recent_outreach(None, 20), admin=True),
    }


@router.get("/dashboard/admin/user/{uid}")
def admin_user_detail(uid: str, admin: Member = Depends(require_admin)) -> Any:
    if not uid or len(uid) > 128 or "/" in uid or uid == store.PLATFORM:
        raise HTTPException(400, detail="Invalid user ID")
    profile = load_profile(uid)
    if profile is None:
        raise HTTPException(404, detail="User not found")
    stats = _stats(uid, True)
    verified_total = stats["verified"]["total"]
    return {
        "user": {
            "id": uid, "name": profile.get("displayName", ""), "email": profile.get("email", ""),
            "role": "admin" if profile.get("isAdmin") is True else "user", "status": profile.get("status", "active"),
        },
        **stats,
        "scout_rate": round((stats["scouted"]["total"] / verified_total) * 100) if verified_total > 0 else 0,
        "recent_scouts": _activity(store.recent_outreach(uid, 20), admin=False),
    }


class ScoutLogRequest(BaseModel):
    batch_number: int = Field(ge=1, le=100_000)
    email_count: int = Field(ge=0, le=10_000)
    subject: str = Field(max_length=300)
    total_batches: int = Field(ge=1, le=100_000)


@router.post("/scout/log")
def log_scout(payload: ScoutLogRequest, member: Member = Depends(require_member)) -> Any:
    store.record_outreach(member.uid, member.name, payload.batch_number, payload.total_batches, payload.email_count, payload.subject)
    return {"message": "Logged"}


@router.get("/admin/scout-logs")
def get_scout_logs(admin: Member = Depends(require_admin)) -> Any:
    return _activity(store.recent_outreach(None, 200), admin=True)
