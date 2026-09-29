"""Email Scouting functionality, scoped to the Firebase UID."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.common import utcnow
from tests.conftest import bearer


def _seed_pool(db: Any, country: str = "US", n: int = 300) -> None:
    db["leads"].insert_many([
        {"email": f"lead{i}@{'bad' if i % 10 == 9 else 'catch' if i % 10 == 8 else 'shop'}{i}.com", "country": country, "domain": "", "status": "available",
         "claimed_by": None, "claimed_by_job": None, "claimed_at": None, "verified_result": None}
        for i in range(n)
    ])


def test_scouting_flow_claim_verify_history_results_export(client: TestClient, db: Any) -> None:
    _seed_pool(db)
    countries = client.get("/leads/countries", headers=bearer("member")).json()
    assert countries[0]["country"] == "US" and countries[0]["available"] == 300

    r = client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "us", "amount": 100})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert r.json()["claimed"] == 100

    # Background task has run: leads claimed by the Firebase UID.
    assert db["leads"].count_documents({"claimed_by": "uid-member"}) == 100
    status = client.get(f"/leads/status/{job_id}", headers=bearer("member")).json()
    assert status["status"] == "done" and status["percent"] == 100 and len(status["results"]) == 100

    history = client.get("/leads/history", headers=bearer("member")).json()
    assert history[0]["id"] == job_id and "user_id" not in history[0] and "results" not in history[0]

    results = client.get(f"/leads/results/{job_id}", headers=bearer("member")).json()["results"]
    cats = {r["category"] for r in results}
    assert cats == {"delivers", "unknown", "bounce"}

    exp = client.get(f"/leads/export/{job_id}?category=delivers", headers=bearer("member"))
    assert exp.status_code == 200
    assert exp.headers["content-disposition"].startswith("attachment; filename=leads_US_delivers_")
    lines = exp.text.splitlines()
    assert lines[0] == "email" and len(lines) - 1 == sum(1 for r in results if r["category"] == "delivers")

    dash = client.get("/dashboard/me", headers=bearer("member")).json()
    assert dash["verified"]["total"] == 100


def test_jobs_are_private_to_their_owner(client: TestClient, db: Any) -> None:
    _seed_pool(db)
    job_id = client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "US", "amount": 100}).json()["job_id"]
    for path in [f"/leads/status/{job_id}", f"/leads/results/{job_id}", f"/leads/export/{job_id}"]:
        r = client.get(path, headers=bearer("member-2"))
        assert r.status_code == 403 and r.json()["detail"] == "Not your job"
    assert client.get("/leads/history", headers=bearer("member-2")).json() == []


def test_claim_validation_and_limits(client: TestClient, db: Any) -> None:
    _seed_pool(db, n=50)
    assert client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "US", "amount": 50}).status_code == 400
    assert client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "../x", "amount": 100}).status_code == 400
    r = client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "GB", "amount": 100})
    assert r.status_code == 404
    db["leads"].insert_many([{"email": f"x{i}@a.com", "country": "ZZ", "status": "claimed", "claimed_by": "uid-member", "claimed_at": utcnow()} for i in range(2950)])
    r = client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "US", "amount": 100})
    assert r.status_code == 429 and "You can claim 50 more today" in r.json()["detail"]


def test_bad_job_ids(client: TestClient) -> None:
    assert client.get("/leads/status/not-an-id", headers=bearer("member")).status_code == 400
    assert client.get("/leads/status/" + "0" * 24, headers=bearer("member")).status_code == 404


def test_single_verify_and_multi(client: TestClient, db: Any) -> None:
    one = client.post("/verify/single", headers=bearer("member"), json={"email": "owner@shop.com"}).json()
    assert one["category"] == "delivers"
    multi = client.post("/verify/single", headers=bearer("member"), json={"email": "a@shop.com; b@bad.com | c@catch.io"}).json()
    assert multi["multiple"] is True and [r["category"] for r in multi["results"]] == ["delivers", "bounce", "unknown"]
    assert db["verify_logs"].count_documents({"user_id": "uid-member"}) == 2
    too_many = "; ".join(f"x{i}@a.com" for i in range(30))
    assert client.post("/verify/single", headers=bearer("member"), json={"email": too_many}).status_code == 400


def test_bulk_verify_is_persisted_and_owned(client: TestClient, db: Any) -> None:
    csv = "store,Email\nA,a@shop.com\nB,b@bad.com\nC,A@shop.com\nD,c@catch.io\n"
    r = client.post("/verify/bulk", headers=bearer("member"), files={"file": ("list.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert r.json()["total"] == 3
    job = db["verify_jobs"].find_one()
    assert job["user_id"] == "uid-member" and job["status"] == "done"

    res = client.get(f"/results/{job_id}", headers=bearer("member")).json()
    assert res["status"] == "done" and res["percent"] == 100 and len(res["results"]) == 3
    assert client.get(f"/results/{job_id}", headers=bearer("member-2")).status_code == 403
    exp = client.get(f"/results/{job_id}/export?category=bounce", headers=bearer("member"))
    assert exp.text.splitlines() == ["email", "b@bad.com"]


def test_bulk_verify_rejects_bad_files(client: TestClient) -> None:
    assert client.post("/verify/bulk", headers=bearer("member"), files={"file": ("x.txt", "a@b.com", "text/plain")}).status_code == 400
    assert client.post("/verify/bulk", headers=bearer("member"), files={"file": ("x.csv", "name\nnobody\n", "text/csv")}).status_code == 400


def test_failed_bulk_job_reads_as_not_found(client: TestClient, db: Any) -> None:
    oid = db["verify_jobs"].insert_one({"user_id": "uid-member", "status": "failed", "progress": 0, "total": 3}).inserted_id
    r = client.get(f"/results/{oid}", headers=bearer("member"))
    assert r.status_code == 404 and "upload the file again" in r.json()["detail"]


def test_scout_log_feeds_dashboard(client: TestClient) -> None:
    for i in (1, 2):
        client.post("/scout/log", headers=bearer("member"), json={"batch_number": i, "total_batches": 2, "email_count": 45, "subject": "Hello"})
    d = client.get("/dashboard/me", headers=bearer("member")).json()
    assert d["scouted"]["total"] == 90 and d["scouted"]["batches_total"] == 2
    assert d["recent_activity"][0]["batch_number"] == 2 and "user_id" not in d["recent_activity"][0]
    assert len(d["verify_series"]) == 30
    other = client.get("/dashboard/me", headers=bearer("member-2")).json()
    assert other["scouted"]["total"] == 0


def test_admin_pool_management(client: TestClient, db: Any) -> None:
    csv = "email,website\nOne@Store.com,store.com\ntwo@store.com,\none@store.com,dup\n"
    r = client.post("/admin/leads/upload/ng?name=Nigeria%20shops", headers=bearer("admin"), files={"file": ("p.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json() == {"message": "Upload complete", "country": "NG", "inserted": 2, "skipped_duplicates": 1, "total_processed": 3}
    assert db["leads"].find_one({"email": "one@store.com"})["uploaded_by"] == "uid-admin"
    stats = client.get("/admin/leads/stats", headers=bearer("admin")).json()
    assert stats == [{"country": "NG", "name": "Nigeria shops", "flag": "", "total": 2, "available": 2, "claimed": 0, "expired": 0}]
    assert client.get("/admin/leads/jobs", headers=bearer("admin")).status_code == 200
    assert client.get("/dashboard/admin", headers=bearer("admin")).status_code == 200
    detail = client.get("/dashboard/admin/user/uid-member", headers=bearer("admin")).json()
    assert detail["user"]["name"] == "Ada Obi"
    assert client.delete("/admin/leads/delete-available/NG", headers=bearer("admin")).json()["deleted"] == 2
    assert client.post("/admin/leads/upload/NG", headers=bearer("member"), files={"file": ("p.csv", csv, "text/csv")}).status_code == 403


def test_errors_never_leak_internals(client: TestClient, monkeypatch: Any) -> None:
    import app.routes.dashboard as dash

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("pymongo.errors.ServerSelectionTimeoutError: mongodb://secret@host")

    monkeypatch.setattr(dash, "_stats", boom)
    c = TestClient(client.app, raise_server_exceptions=False)
    r = c.get("/dashboard/me", headers=bearer("member"))
    assert r.status_code == 500 and r.json() == {"detail": "Internal server error"}


def test_cors_allows_scout_origin_only(client: TestClient) -> None:
    ok = client.options("/dashboard/me", headers={"Origin": "https://scout.eliteachieversteam.com", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "https://scout.eliteachieversteam.com"
    assert "access-control-allow-credentials" not in ok.headers
    bad = client.options("/dashboard/me", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers
    simple = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers
