"""Single and bulk verification (MailClean /verify/*, /results/*).
Bulk jobs live in Firestore `email_verifications/{id}` (+ results)."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app import store
from app.auth import Member, require_member
from app.common import EMAIL_RE, csv_response, emails_from_csv, failed_result, filter_category, log_verification, owned_job, percent, run_verify
from app.config import settings

router = APIRouter()

MAX_SINGLE_BATCH = 25
BULK_STALE_AFTER = timedelta(hours=3)
FLUSH_EVERY = 50


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

    job_id = store.create_doc(store.VERIFICATIONS, {
        "userId": member.uid, "userName": member.name, "status": "processing",
        "progress": 0, "total": len(emails), "createdAt": store.now(), "completedAt": None,
    })
    background_tasks.add_task(run_bulk_job, job_id, emails, member)
    return {"job_id": job_id, "total": len(emails)}


def run_bulk_job(job_id: str, emails: list[str], member: Member) -> None:
    pending: list[dict[str, Any]] = []
    written = 0
    try:
        with ThreadPoolExecutor(max_workers=settings.max_threads) as executor:
            futures = {executor.submit(run_verify, e): e for e in emails}
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    pending.append(future.result())
                except Exception:
                    pending.append(failed_result(futures[future]))
                if len(pending) >= FLUSH_EVERY or i == len(emails):
                    store.write_results(store.VERIFICATIONS, job_id, written + 1, pending)
                    written += len(pending)
                    pending = []
                    store.update_doc(store.VERIFICATIONS, job_id, {"progress": i})
        store.update_doc(store.VERIFICATIONS, job_id, {"status": "done", "completedAt": store.now()})
        log_verification(member, len(emails), "bulk")
    except Exception:
        store.update_doc(store.VERIFICATIONS, job_id, {"status": "failed", "completedAt": store.now()})


def _bulk_job(job_id: str, member: Member) -> dict[str, Any]:
    job = owned_job(store.VERIFICATIONS, job_id, member)
    created = job.get("createdAt")
    stale = job["status"] == "processing" and created is not None and store.now() - created > BULK_STALE_AFTER
    if job["status"] == "failed" or stale:
        # The frontend shows 404 details as a friendly "not found" state.
        raise HTTPException(404, detail="This verification job didn't finish. Please upload the file again.")
    return job


@router.get("/results/{job_id}")
def get_results(job_id: str, member: Member = Depends(require_member)) -> Any:
    job = _bulk_job(job_id, member)
    done = job["status"] == "done"
    progress, total = int(job.get("progress", 0)), int(job.get("total", 0))
    return {
        "job_id": job_id, "status": job["status"], "progress": progress, "total": total, "percent": percent(progress, total),
        "results": store.read_results(store.VERIFICATIONS, job_id) if done else [],
    }


@router.get("/results/{job_id}/export")
def export_results(job_id: str, category: str = "all", member: Member = Depends(require_member)) -> Any:
    job = _bulk_job(job_id, member)
    if job["status"] != "done":
        raise HTTPException(400, detail="Job still processing")
    emails = [r["email"] for r in filter_category(store.read_results(store.VERIFICATIONS, job_id), category) if r.get("email")]
    return csv_response(emails, f"email-scouting_{category}_{job_id[:8]}.csv")
