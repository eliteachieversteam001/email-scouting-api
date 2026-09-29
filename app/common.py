"""Helpers shared by the routes (CSV parsing, exports, job lookups, logging)."""

from __future__ import annotations

import io
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app import store
from app.auth import Member
from app.verifier import EmailVerifier

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
HEADER_RE = re.compile(r"mail|email|gmail|contact|recipient|address|receiver", re.IGNORECASE)

_verifier: EmailVerifier | None = None


def _default_verify(email: str) -> dict[str, Any]:
    global _verifier
    if _verifier is None:
        _verifier = EmailVerifier()
    return _verifier.verify(email)


# Swappable in tests; production uses the MailClean verifier.
verify_email: Callable[[str], dict[str, Any]] = _default_verify


def run_verify(email: str) -> dict[str, Any]:
    return verify_email(email)


def iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def failed_result(email: str) -> dict[str, Any]:
    # MailClean stored str(exception) here; never send exception text to clients.
    return {
        "email": email, "error": "verification_failed", "risk": "HIGH", "category": "bounce", "sendable": False,
        "smtp_valid": False, "mx_valid": False, "format_valid": True, "catch_all": False, "disposable": False,
        "message": "Verification failed on the server",
    }


JOB_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")


def check_job_id(job_id: str) -> str:
    """Firestore auto-IDs only — also blocks path tricks like 'a/b'."""
    if not JOB_ID_RE.match(job_id or ""):
        raise HTTPException(400, detail="Invalid job ID")
    return job_id


def owned_job(collection: str, job_id: str, member: Member) -> dict[str, Any]:
    job = store.get_doc(collection, check_job_id(job_id))
    if not job:
        raise HTTPException(404, detail="Job not found")
    if job.get("userId") != member.uid:
        raise HTTPException(403, detail="Not your job")
    return job


def percent(progress: int, total: int) -> int:
    return round((progress / total) * 100) if total > 0 else 0


def emails_from_csv(contents: bytes) -> list[str]:
    """MailClean smart column detection, unchanged."""
    try:
        df = pd.read_csv(io.BytesIO(contents))
    except Exception:
        raise HTTPException(400, detail="Could not parse CSV.")
    if df.empty or len(df.columns) == 0:
        raise HTTPException(400, detail="No emails found in CSV")

    target_col = None
    for col in df.columns:
        if HEADER_RE.search(str(col)):
            target_col = col
            break
    if target_col is None:
        best_col, best_count = df.columns[0], 0
        for col in df.columns:
            count = df[col].dropna().astype(str).apply(lambda x: bool(EMAIL_RE.search(x))).sum()
            if count > best_count:
                best_count, best_col = count, col
        target_col = best_col

    values = df[target_col].dropna().astype(str).tolist()
    for col in df.columns:
        if col != target_col:
            values.extend(v for v in df[col].dropna().astype(str) if EMAIL_RE.search(v))

    seen: set[str] = set()
    out: list[str] = []
    for entry in values:
        for e in EMAIL_RE.findall(entry):
            e = e.lower().strip()
            if e not in seen:
                seen.add(e)
                out.append(e)
    return out


def filter_category(results: Iterable[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    if category in ("delivers", "unknown", "bounce"):
        return [r for r in results if r.get("category") == category]
    return list(results)


def csv_response(emails: list[str], filename: str) -> StreamingResponse:
    body = "email\n" + "\n".join(emails)
    safe = re.sub(r"[^\w.\-]", "_", filename)
    return StreamingResponse(iter([body]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={safe}"})


def log_verification(member: Member, email_count: int, source: str) -> None:
    store.record_verification(member.uid, member.name, email_count, source)
