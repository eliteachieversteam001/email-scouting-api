"""Lead scouting: pools, claim-and-verify jobs, history, results, exports.
Ported from MailClean routes_leads.py onto Firestore; identity = Firebase UID."""

from __future__ import annotations

import io
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app import store
from app.auth import Member, require_admin, require_member
from app.common import EMAIL_RE, csv_response, failed_result, filter_category, iso, owned_job, percent, run_verify
from app.config import settings

router = APIRouter()

COUNTRY_META = {
    "US": {"name": "United States", "flag": "🇺🇸"},
    "GB": {"name": "United Kingdom", "flag": "🇬🇧"},
    "CA": {"name": "Canada", "flag": "🇨🇦"},
    "AU": {"name": "Australia", "flag": "🇦🇺"},
}
CODE_RE = re.compile(r"^[A-Z0-9]{2,10}$")
ABANDON_AFTER = timedelta(minutes=45)
PROGRESS_EVERY = 25


def get_meta(code: str) -> dict[str, str]:
    if code in COUNTRY_META:
        return COUNTRY_META[code]
    pool = store.get_pool(code) or {}
    return {"name": pool.get("name") or code, "flag": pool.get("flag", "")}


def pool_code(country: str) -> str:
    code = country.upper().strip()
    if not CODE_RE.match(code):
        raise HTTPException(400, detail="Invalid country code")
    return code


def expire_abandoned_jobs() -> None:
    """Return claimed-but-unverified leads to the pool after 45 minutes."""
    for job in store.abandoned_jobs(ABANDON_AFTER):
        store.release_unverified_leads(job["id"])
        store.update_doc(store.JOBS, job["id"], {"status": "expired"})


def _job_summary(job: dict[str, Any], *, admin: bool) -> dict[str, Any]:
    out = {
        "id": job["id"], "country": job.get("country", ""), "amount": job.get("amount", 0), "status": job.get("status"),
        "progress": job.get("progress", 0), "total": job.get("total", 0),
        "created_at": iso(job.get("createdAt")), "completed_at": iso(job.get("completedAt")),
    }
    if admin:
        out.update({"user_id": job.get("userId"), "user_name": job.get("userName", "")})
    return out


# ── Admin: pools ──────────────────────────────────────────────────────────────

@router.post("/admin/leads/upload/{country}")
async def upload_leads(country: str, request: Request, file: UploadFile = File(...), admin: Member = Depends(require_admin)) -> Any:
    code = pool_code(country)
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, detail="Only .csv files accepted")
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

    rows: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        cell = str(row.get(email_col, "") or "")
        domain = str(row.get(domain_col, "") or "") if domain_col is not None else ""
        domain = "" if domain == "nan" else domain[:255]
        rows.extend((e, domain) for e in dict.fromkeys(e.lower().strip() for e in EMAIL_RE.findall(cell)))

    custom_name = request.query_params.get("name", "").strip()[:60]
    store.upsert_pool(code, custom_name if code not in COUNTRY_META else None, admin.uid)
    inserted, skipped = store.insert_leads(code, rows, admin.uid)
    return {"message": "Upload complete", "country": code, "inserted": inserted, "skipped_duplicates": skipped, "total_processed": inserted + skipped}


@router.get("/admin/leads/stats")
def leads_stats(admin: Member = Depends(require_admin)) -> Any:
    expire_abandoned_jobs()
    out = []
    for code in store.pool_codes():
        meta = get_meta(code)
        out.append({"country": code, "name": meta["name"], "flag": meta["flag"], **store.pool_counts(code)})
    return out


@router.delete("/admin/leads/delete/{country}")
def delete_country_leads(country: str, admin: Member = Depends(require_admin)) -> Any:
    code = pool_code(country)
    return {"deleted": store.delete_leads(code, available_only=False), "country": code}


@router.delete("/admin/leads/delete-available/{country}")
def delete_available_leads(country: str, admin: Member = Depends(require_admin)) -> Any:
    code = pool_code(country)
    return {"deleted": store.delete_leads(code, available_only=True), "country": code}


@router.get("/admin/leads/sample/{country}")
def get_sample_leads(country: str, admin: Member = Depends(require_admin)) -> Any:
    return store.sample_leads(pool_code(country))


@router.get("/admin/leads/jobs")
def get_all_lead_jobs(admin: Member = Depends(require_admin)) -> Any:
    return [_job_summary(j, admin=True) for j in store.all_jobs(100)]


# ── Members ───────────────────────────────────────────────────────────────────

@router.get("/leads/countries")
def get_countries(member: Member = Depends(require_member)) -> Any:
    out = []
    for code in store.pool_codes():
        available = store.available_count(code)
        if available > 0:
            meta = get_meta(code)
            out.append({"country": code, "name": meta["name"], "flag": meta["flag"], "available": available})
    return sorted(out, key=lambda x: x["available"], reverse=True)


class ClaimRequest(BaseModel):
    country: str
    amount: int


@router.post("/leads/claim-and-verify")
def claim_and_verify(payload: ClaimRequest, background_tasks: BackgroundTasks, member: Member = Depends(require_member)) -> Any:
    expire_abandoned_jobs()
    country = pool_code(payload.country)
    if payload.amount < 100 or payload.amount > 2000:
        raise HTTPException(400, detail="Amount must be between 100 and 2000")

    used = store.claimed_today(member.uid)
    if used + payload.amount > settings.max_claim_per_day:
        remaining = max(0, settings.max_claim_per_day - used)
        raise HTTPException(429, detail=f"Daily limit reached. You can claim {remaining} more today.")
    if store.user_has_active_job(member.uid):
        raise HTTPException(400, detail="You have a job already running. Wait for it to complete or check your history.")

    job_id = store.create_doc(store.JOBS, {
        "userId": member.uid, "userName": member.name, "country": country, "amount": payload.amount,
        "status": "processing", "progress": 0, "total": 0, "createdAt": store.now(), "completedAt": None,
    })
    claimed = store.claim_leads(country, payload.amount, member.uid, job_id)
    if not claimed:
        store.update_doc(store.JOBS, job_id, {"status": "failed", "error": "No available leads"})
        raise HTTPException(404, detail=f"No available leads for {country}. Admin needs to upload more.")

    store.update_doc(store.JOBS, job_id, {"total": len(claimed)})
    store.record_claim(member.uid, len(claimed))
    background_tasks.add_task(run_lead_verification, job_id, country, claimed, member)
    return {"job_id": job_id, "claimed": len(claimed), "country": country}


def run_lead_verification(job_id: str, country: str, emails: list[str], member: Member) -> None:
    results: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=settings.max_threads) as executor:
            futures = {executor.submit(run_verify, e): e for e in emails}
            for i, future in enumerate(as_completed(futures), 1):
                email = futures[future]
                try:
                    result = future.result()
                    store.mark_lead_verified(country, email, job_id, result)
                except Exception:
                    result = failed_result(email)
                results.append(result)
                if i % PROGRESS_EVERY == 0 or i == len(emails):
                    store.update_doc(store.JOBS, job_id, {"progress": i})
        store.write_results(store.JOBS, job_id, 1, results)
        store.update_doc(store.JOBS, job_id, {"status": "done", "completedAt": store.now(), "progress": len(emails)})
        store.record_verification(member.uid, member.name, len(emails), "scout_leads")
    except Exception:
        store.update_doc(store.JOBS, job_id, {"status": "failed", "completedAt": store.now()})


def _job_payload(job: dict[str, Any], with_results: bool) -> dict[str, Any]:
    progress, total = int(job.get("progress", 0)), int(job.get("total", 0))
    return {
        "job_id": job["id"], "status": job["status"], "progress": progress, "total": total,
        "percent": percent(progress, total), "country": job.get("country", ""),
        "results": store.read_results(store.JOBS, job["id"]) if with_results else [],
    }


@router.get("/leads/status/{job_id}")
def lead_job_status(job_id: str, member: Member = Depends(require_member)) -> Any:
    job = owned_job(store.JOBS, job_id, member)
    return _job_payload(job, job["status"] == "done")


@router.get("/leads/history")
def my_lead_history(member: Member = Depends(require_member)) -> Any:
    return [_job_summary(j, admin=False) for j in store.user_jobs(member.uid, 20)]


@router.get("/leads/results/{job_id}")
def get_lead_results(job_id: str, member: Member = Depends(require_member)) -> Any:
    job = owned_job(store.JOBS, job_id, member)
    if job["status"] != "done":
        raise HTTPException(400, detail="Job not complete yet")
    return _job_payload(job, True)


@router.get("/leads/export/{job_id}")
def export_lead_emails(job_id: str, category: str = "all", member: Member = Depends(require_member)) -> Any:
    job = owned_job(store.JOBS, job_id, member)
    if job["status"] != "done":
        raise HTTPException(400, detail="Job still processing")
    emails = [r["email"] for r in filter_category(store.read_results(store.JOBS, job["id"]), category) if r.get("email")]
    return csv_response(emails, f"leads_{job.get('country', 'XX')}_{category}_{job_id[:8]}.csv")
