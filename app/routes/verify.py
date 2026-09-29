"""Single and bulk verification (MailClean /verify/*, /results/*)."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.auth import Member, require_member
from app.common import (
    EMAIL_RE,
    csv_response,
    emails_from_csv,
    failed_result,
    filter_category,
    log_verification,
    owned_job,
    percent,
    run_verify,
    utcnow,
)
from app.config import settings
from app.db import get_db

router = APIRouter()

MAX_SINGLE_BATCH = 25
BULK_STALE_AFTER = timedelta(hours=3)


class SingleEmailRequest(BaseModel):
    email: str


@router.post("/verify/single")
def verify_single(payload: SingleEmailRequest, member: Member = Depends(require_member)) -> Any:
    raw = payload.email.strip()
    if not raw or len(raw) > 5000:
        raise HTTPException(400, detail="Enter an email address to verify")
    parts = [p.strip().lower() for p in re.split(r"[;:|]+", raw) if EMAIL_RE.search(p.strip())]
    if len(parts) > MAX_SINGLE_BATCH:
        raise HTTPException(400, detail=f"Verify up to {MAX_SINGLE_BATCH} emails at once, or use bulk CSV")
    if len(parts) > 1:
        results = [run_verify(e) for e in parts]
        log_verification(member, len(parts), "single_multi")
        return {"multiple": True, "results": results}
    result = run_verify(raw)
    log_verification(member, 1, "single")
    return result


@router.post("/verify/bulk")
async def verify_bulk(background_tasks: BackgroundTasks, file: UploadFile = File(...), member: Member = Depends(require_member)) -> Any:
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, detail="Only .csv files are accepted")
    contents = await file.read(settings.max_upload_bytes + 1)
    if len(contents) > settings.max_upload_bytes:
        raise HTTPException(400, detail="That file is too large")
    emails = emails_from_csv(contents)
    if not emails:
        raise HTTPException(400, detail="No emails found in CSV")
    if len(emails) > settings.max_bulk_emails:
        raise HTTPException(400, detail=f"Max {settings.max_bulk_emails:,} emails per job")

    db = get_db()
    job_id = db["verify_jobs"].insert_one({
        "user_id": member.uid, "user_name": member.name, "user_email": member.email,
        "status": "processing", "progress": 0, "total": len(emails),
        "created_at": utcnow(), "completed_at": None,
    }).inserted_id
    background_tasks.add_task(run_bulk_job, str(job_id), emails, member)
    return {"job_id": str(job_id), "total": len(emails)}


def run_bulk_job(job_id: str, emails: list[str], member: Member) -> None:
    db = get_db()
    oid = ObjectId(job_id)
    rows: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=settings.max_threads) as executor:
            futures = {executor.submit(run_verify, e): e for e in emails}
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    result = future.result()
                except Exception:
                    result = failed_result(futures[future])
                rows.append({"job_id": job_id, "idx": i, "result": result})
                if len(rows) >= 50 or i == len(emails):
                    db["verify_results"].insert_many(rows)
                    rows = []
                    db["verify_jobs"].update_one({"_id": oid}, {"$set": {"progress": i}})
        db["verify_jobs"].update_one({"_id": oid}, {"$set": {"status": "done", "completed_at": utcnow()}})
        log_verification(member, len(emails), "bulk")
    except Exception:
        db["verify_jobs"].update_one({"_id": oid}, {"$set": {"status": "failed", "completed_at": utcnow()}})


def _bulk_job(job_id: str, member: Member) -> dict[str, Any]:
    job = owned_job("verify_jobs", job_id, member)
    stale = job["status"] == "processing" and job.get("created_at") and utcnow() - job["created_at"] > BULK_STALE_AFTER
    if job["status"] == "failed" or stale:
        # The frontend shows 404 details as a friendly "not found" state.
        raise HTTPException(404, detail="This verification job didn't finish. Please upload the file again.")
    return job


def _bulk_results(job_id: str) -> list[dict[str, Any]]:
    cur = get_db()["verify_results"].find({"job_id": job_id}, {"_id": 0, "result": 1}).sort("idx", 1)
    return [r["result"] for r in cur]


@router.get("/results/{job_id}")
def get_results(job_id: str, member: Member = Depends(require_member)) -> Any:
    job = _bulk_job(job_id, member)
    done = job["status"] == "done"
    return {
        "job_id": job_id, "status": job["status"], "progress": job.get("progress", 0), "total": job.get("total", 0),
        "percent": percent(job.get("progress", 0), job.get("total", 0)),
        "results": _bulk_results(job_id) if done else [],
    }


@router.get("/results/{job_id}/export")
def export_results(job_id: str, category: str = "all", member: Member = Depends(require_member)) -> Any:
    job = _bulk_job(job_id, member)
    if job["status"] != "done":
        raise HTTPException(400, detail="Job still processing")
    emails = [r["email"] for r in filter_category(_bulk_results(job_id), category) if r.get("email")]
    return csv_response(emails, f"email-scouting_{category}_{job_id[:8]}.csv")
