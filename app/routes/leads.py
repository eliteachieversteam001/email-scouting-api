"""Lead scouting: pools, claim-and-verify jobs, history, results, exports.
Ported from MailClean routes_leads.py; identity is the Firebase UID."""

from __future__ import annotations

import io
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any

import pandas as pd
from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.auth import Member, require_admin, require_member
from app.common import EMAIL_RE, csv_response, failed_result, filter_category, iso, owned_job, percent, run_verify, utcnow
from app.config import settings
from app.db import get_db

router = APIRouter()

COUNTRY_META = {
    "US": {"name": "United States", "flag": "🇺🇸"},
    "GB": {"name": "United Kingdom", "flag": "🇬🇧"},
    "CA": {"name": "Canada", "flag": "🇨🇦"},
    "AU": {"name": "Australia", "flag": "🇦🇺"},
}
CODE_RE = re.compile(r"^[A-Z0-9]{2,10}$")
ABANDON_AFTER = timedelta(minutes=45)


def get_meta(country: str) -> dict[str, str]:
    if country in COUNTRY_META:
        return COUNTRY_META[country]
    stored = get_db()["country_meta"].find_one({"code": country})
    if stored:
        return {"name": stored.get("name", country), "flag": stored.get("flag", "")}
    return {"name": country, "flag": ""}


def pool_code(country: str) -> str:
    code = country.upper().strip()
    if not CODE_RE.match(code):
        raise HTTPException(400, detail="Invalid country code")
    return code


def expire_abandoned_jobs() -> None:
    """Return claimed-but-unverified leads to the pool after 45 minutes."""
    db = get_db()
    cutoff = utcnow() - ABANDON_AFTER
    for job in db["lead_jobs"].find({"status": "processing", "created_at": {"$lt": cutoff}}):
        db["leads"].update_many(
            {"claimed_by_job": str(job["_id"]), "verified_result": None},
            {"$set": {"status": "available", "claimed_by": None, "claimed_by_job": None, "claimed_at": None}},
        )
        db["lead_jobs"].update_one({"_id": job["_id"]}, {"$set": {"status": "expired"}})


def _serialize_job(j: dict[str, Any]) -> dict[str, Any]:
    j["id"] = str(j.pop("_id"))
    j.pop("user_email", None)  # not needed by the client
    j["created_at"] = iso(j.get("created_at"))
    j["completed_at"] = iso(j.get("completed_at"))
    return j


# ── Admin: pools ──────────────────────────────────────────────────────────────

@router.post("/admin/leads/upload/{country}")
async def upload_leads(country: str, request: Request, file: UploadFile = File(...), admin: Member = Depends(require_admin)) -> Any:
    code = pool_code(country)
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, detail="Only .csv files accepted")
    db = get_db()
    custom_name = request.query_params.get("name", "").strip()[:60]
    if custom_name and code not in COUNTRY_META:
        db["country_meta"].update_one({"code": code}, {"$set": {"code": code, "name": custom_name, "flag": ""}}, upsert=True)

    contents = await file.read(settings.max_upload_bytes + 1)
    if len(contents) > settings.max_upload_bytes:
        raise HTTPException(400, detail="That file is too large")
    try:
        df = pd.read_csv(io.BytesIO(contents))
    except Exception:
        raise HTTPException(400, detail="Could not parse CSV")
    if df.empty:
        raise HTTPException(400, detail="No emails found in CSV")

    email_col = next((c for c in df.columns if re.search(r"mail|email|gmail|contact|recipient", str(c), re.IGNORECASE)), df.columns[0])
    domain_col = next((c for c in df.columns if re.search(r"domain|url|website|store", str(c), re.IGNORECASE)), None)

    inserted = skipped = 0
    now = utcnow()
    for _, row in df.iterrows():
        cell = str(row.get(email_col, "") or "")
        domain = str(row.get(domain_col, "") or "") if domain_col is not None else ""
        if domain == "nan":
            domain = ""
        for email in {e.lower().strip() for e in EMAIL_RE.findall(cell)}:
            try:
                db["leads"].insert_one({
                    "email": email, "country": code, "domain": domain[:255], "status": "available",
                    "claimed_by": None, "claimed_by_job": None, "claimed_at": None, "verified_result": None,
                    "uploaded_at": now, "uploaded_by": admin.uid,
                })
                inserted += 1
            except DuplicateKeyError:
                skipped += 1
    return {"message": "Upload complete", "country": code, "inserted": inserted, "skipped_duplicates": skipped, "total_processed": inserted + skipped}


@router.get("/admin/leads/stats")
def leads_stats(admin: Member = Depends(require_admin)) -> Any:
    expire_abandoned_jobs()
    leads = get_db()["leads"]
    stats = []
    for country in sorted(leads.distinct("country")):
        meta = get_meta(country)
        stats.append({
            "country": country, "name": meta["name"], "flag": meta["flag"],
            "total": leads.count_documents({"country": country}),
            "available": leads.count_documents({"country": country, "status": "available"}),
            "claimed": leads.count_documents({"country": country, "status": "claimed"}),
            "expired": leads.count_documents({"country": country, "status": "expired"}),
        })
    return stats


@router.delete("/admin/leads/delete/{country}")
def delete_country_leads(country: str, admin: Member = Depends(require_admin)) -> Any:
    code = pool_code(country)
    return {"deleted": get_db()["leads"].delete_many({"country": code}).deleted_count, "country": code}


@router.delete("/admin/leads/delete-available/{country}")
def delete_available_leads(country: str, admin: Member = Depends(require_admin)) -> Any:
    code = pool_code(country)
    return {"deleted": get_db()["leads"].delete_many({"country": code, "status": "available"}).deleted_count, "country": code}


@router.get("/admin/leads/sample/{country}")
def get_sample_leads(country: str, admin: Member = Depends(require_admin)) -> Any:
    code = pool_code(country)
    return list(get_db()["leads"].find({"country": code}, {"_id": 0, "email": 1, "domain": 1, "status": 1}).limit(10))


@router.get("/admin/leads/jobs")
def get_all_lead_jobs(admin: Member = Depends(require_admin)) -> Any:
    jobs = get_db()["lead_jobs"].find({}, {"results": 0}).sort("created_at", -1).limit(100)
    return [_serialize_job(j) for j in jobs]


# ── Members ───────────────────────────────────────────────────────────────────

@router.get("/leads/countries")
def get_countries(member: Member = Depends(require_member)) -> Any:
    leads = get_db()["leads"]
    countries = []
    for country in leads.distinct("country"):
        available = leads.count_documents({"country": country, "status": "available"})
        if available > 0:
            meta = get_meta(country)
            countries.append({"country": country, "name": meta["name"], "flag": meta["flag"], "available": available})
    return sorted(countries, key=lambda x: x["available"], reverse=True)


class ClaimRequest(BaseModel):
    country: str
    amount: int


@router.post("/leads/claim-and-verify")
def claim_and_verify(payload: ClaimRequest, background_tasks: BackgroundTasks, member: Member = Depends(require_member)) -> Any:
    expire_abandoned_jobs()
    country = pool_code(payload.country)
    if payload.amount < 100 or payload.amount > 2000:
        raise HTTPException(400, detail="Amount must be between 100 and 2000")

    db = get_db()
    day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    claimed_today = db["leads"].count_documents({"claimed_by": member.uid, "claimed_at": {"$gte": day_start}})
    if claimed_today + payload.amount > settings.max_claim_per_day:
        remaining = max(0, settings.max_claim_per_day - claimed_today)
        raise HTTPException(429, detail=f"Daily limit reached. You can claim {remaining} more today.")

    if db["lead_jobs"].find_one({"user_id": member.uid, "status": "processing"}):
        raise HTTPException(400, detail="You have a job already running. Wait for it to complete or check your history.")

    job_oid = db["lead_jobs"].insert_one({
        "user_id": member.uid, "user_name": member.name, "user_email": member.email,
        "country": country, "amount": payload.amount, "status": "processing",
        "progress": 0, "total": 0, "results": [], "created_at": utcnow(), "completed_at": None,
    }).inserted_id
    job_id = str(job_oid)

    claimed: list[str] = []
    claimed_at = utcnow()
    for doc in db["leads"].find({"country": country, "status": "available"}, {"_id": 1}).limit(payload.amount):
        res = db["leads"].find_one_and_update(
            {"_id": doc["_id"], "status": "available"},
            {"$set": {"status": "claimed", "claimed_by": member.uid, "claimed_by_job": job_id, "claimed_at": claimed_at}},
            return_document=ReturnDocument.AFTER,
        )
        if res:
            claimed.append(res["email"])

    if not claimed:
        db["lead_jobs"].update_one({"_id": job_oid}, {"$set": {"status": "failed", "error": "No available leads"}})
        raise HTTPException(404, detail=f"No available leads for {country}. Admin needs to upload more.")

    db["lead_jobs"].update_one({"_id": job_oid}, {"$set": {"total": len(claimed)}})
    background_tasks.add_task(run_lead_verification, job_id, claimed, member)
    return {"job_id": job_id, "claimed": len(claimed), "country": country}


def run_lead_verification(job_id: str, emails: list[str], member: Member) -> None:
    db = get_db()
    oid = ObjectId(job_id)
    results: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=settings.max_threads) as executor:
            futures = {executor.submit(run_verify, e): e for e in emails}
            for i, future in enumerate(as_completed(futures), 1):
                email = futures[future]
                try:
                    result = future.result()
                    db["leads"].update_one({"email": email, "claimed_by_job": job_id}, {"$set": {"verified_result": result, "verified_at": utcnow()}})
                except Exception:
                    result = failed_result(email)
                results.append(result)
                if i % 25 == 0 or i == len(emails):
                    db["lead_jobs"].update_one({"_id": oid}, {"$set": {"progress": i}})
        db["lead_jobs"].update_one({"_id": oid}, {"$set": {"status": "done", "completed_at": utcnow(), "results": results, "progress": len(emails)}})
        db["verify_logs"].insert_one({
            "user_id": member.uid, "user_name": member.name, "user_email": member.email,
            "email_count": len(emails), "source": "scout_leads", "sent_at": utcnow(),
        })
    except Exception:
        db["lead_jobs"].update_one({"_id": oid}, {"$set": {"status": "failed", "completed_at": utcnow()}})


def _job_payload(job_id: str, job: dict[str, Any], with_results: bool) -> dict[str, Any]:
    return {
        "job_id": job_id, "status": job["status"], "progress": job.get("progress", 0), "total": job.get("total", 0),
        "percent": percent(job.get("progress", 0), job.get("total", 0)), "country": job.get("country", ""),
        "results": job.get("results", []) if with_results else [],
    }


@router.get("/leads/status/{job_id}")
def lead_job_status(job_id: str, member: Member = Depends(require_member)) -> Any:
    job = owned_job("lead_jobs", job_id, member)
    return _job_payload(job_id, job, job["status"] == "done")


@router.get("/leads/history")
def my_lead_history(member: Member = Depends(require_member)) -> Any:
    jobs = get_db()["lead_jobs"].find({"user_id": member.uid}, {"results": 0, "user_id": 0}).sort("created_at", -1).limit(20)
    return [_serialize_job(j) for j in jobs]


@router.get("/leads/results/{job_id}")
def get_lead_results(job_id: str, member: Member = Depends(require_member)) -> Any:
    job = owned_job("lead_jobs", job_id, member)
    if job["status"] != "done":
        raise HTTPException(400, detail="Job not complete yet")
    return _job_payload(job_id, job, True)


@router.get("/leads/export/{job_id}")
def export_lead_emails(job_id: str, category: str = "all", member: Member = Depends(require_member)) -> Any:
    job = owned_job("lead_jobs", job_id, member)
    if job["status"] != "done":
        raise HTTPException(400, detail="Job still processing")
    emails = [r["email"] for r in filter_category(job.get("results", []), category) if r.get("email")]
    return csv_response(emails, f"leads_{job.get('country', 'XX')}_{category}_{job_id[:8]}.csv")
