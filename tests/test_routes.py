"""Email Scouting functionality on Firestore, scoped to the Firebase UID."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app import store
from tests.conftest import bearer, docs


def seed_pool(code: str = "US", n: int = 300) -> None:
    store.upsert_pool(code, None, "uid-admin")
    rows = [(f"lead{i}@{'bad' if i % 10 == 9 else 'catch' if i % 10 == 8 else 'shop'}{i}.com", "") for i in range(n)]
    store.insert_leads(code, rows, "uid-admin")


def test_scouting_flow_claim_verify_history_results_export(client: TestClient, db: Any) -> None:
    seed_pool()
    countries = client.get("/leads/countries", headers=bearer("member")).json()
    assert countries == [{"country": "US", "name": "United States", "flag": "🇺🇸", "available": 300}]

    r = client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "us", "amount": 100})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert r.json()["claimed"] == 100

    # Firestore: leads claimed by the Firebase UID, job owned by it.
    claimed = docs(db, "scouting_leads", claimedBy="uid-member")
    assert len(claimed) == 100 and all(d["claimedByJob"] == job_id and d["verifiedResult"] for d in claimed)
    job = db.collection("scouting_jobs").document(job_id).get().to_dict()
    assert job["userId"] == "uid-member" and job["status"] == "done" and job["progress"] == 100

    status = client.get(f"/leads/status/{job_id}", headers=bearer("member")).json()
    assert status["status"] == "done" and status["percent"] == 100 and len(status["results"]) == 100

    history = client.get("/leads/history", headers=bearer("member")).json()
    assert history[0]["id"] == job_id and history[0]["country"] == "US" and "user_id" not in history[0]
    assert history[0]["created_at"]

    results = client.get(f"/leads/results/{job_id}", headers=bearer("member")).json()["results"]
    assert {r["category"] for r in results} == {"delivers", "unknown", "bounce"}

    exp = client.get(f"/leads/export/{job_id}?category=delivers", headers=bearer("member"))
    assert exp.status_code == 200
    assert exp.headers["content-disposition"].startswith("attachment; filename=leads_US_delivers_")
    lines = exp.text.splitlines()
    assert lines[0] == "email" and len(lines) - 1 == sum(1 for r in results if r["category"] == "delivers")

    dash = client.get("/dashboard/me", headers=bearer("member")).json()
    assert dash["verified"]["total"] == 100 and dash["verified"]["today"] == 100
    assert client.get("/leads/countries", headers=bearer("member")).json()[0]["available"] == 200


def test_jobs_are_private_to_their_owner(client: TestClient, db: Any) -> None:
    seed_pool()
    job_id = client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "US", "amount": 100}).json()["job_id"]
    for path in [f"/leads/status/{job_id}", f"/leads/results/{job_id}", f"/leads/export/{job_id}"]:
        r = client.get(path, headers=bearer("member-2"))
        assert r.status_code == 403 and r.json()["detail"] == "Not your job"
    assert client.get("/leads/history", headers=bearer("member-2")).json() == []
    assert client.get("/dashboard/me", headers=bearer("member-2")).json()["verified"]["total"] == 0


def test_claim_rules(client: TestClient, db: Any) -> None:
    seed_pool(n=150)
    assert client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "US", "amount": 50}).status_code == 400
    assert client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "../x", "amount": 100}).status_code == 400
    assert client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "GB", "amount": 100}).status_code == 404
    store.record_claim("uid-member", 2950)
    r = client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "US", "amount": 100})
    assert r.status_code == 429 and "You can claim 50 more today" in r.json()["detail"]


def test_one_active_job_at_a_time(client: TestClient, db: Any) -> None:
    seed_pool()
    store.create_doc(store.JOBS, {"userId": "uid-member", "status": "processing", "createdAt": store.now(), "country": "US"})
    r = client.post("/leads/claim-and-verify", headers=bearer("member"), json={"country": "US", "amount": 100})
    assert r.status_code == 400 and "already running" in r.json()["detail"]


def test_abandoned_jobs_release_their_leads(client: TestClient, db: Any) -> None:
    seed_pool(n=120)
    old = store.now() - store.timedelta(hours=2)
    job_id = store.create_doc(store.JOBS, {"userId": "uid-member-2", "status": "processing", "createdAt": old, "country": "US"})
    assert len(store.claim_leads("US", 100, "uid-member-2", job_id)) == 100
    assert store.available_count("US") == 20
    client.get("/admin/leads/stats", headers=bearer("admin"))  # triggers expiry
    assert store.available_count("US") == 120
    assert db.collection("scouting_jobs").document(job_id).get().to_dict()["status"] == "expired"


def test_bad_job_ids(client: TestClient, db: Any) -> None:
    assert client.get("/leads/status/not-an-id", headers=bearer("member")).status_code == 400
    assert client.get("/leads/status/a%2Fb", headers=bearer("member")).status_code in (400, 404)
    assert client.get("/leads/status/" + "A" * 20, headers=bearer("member")).status_code == 404


def test_single_verify_and_multi(client: TestClient, db: Any) -> None:
    one = client.post("/verify/single", headers=bearer("member"), json={"email": "owner@shop.com"}).json()
    assert one["category"] == "delivers"
    multi = client.post("/verify/single", headers=bearer("member"), json={"email": "a@shop.com; b@bad.com | c@catch.io"}).json()
    assert multi["multiple"] is True and [r["category"] for r in multi["results"]] == ["delivers", "bounce", "unknown"]
    events = docs(db, "scouting_history", userId="uid-member")
    assert sorted((e["source"], e["emailCount"]) for e in events) == [("single", 1), ("single_multi", 3)]
    assert db.collection("scouting_usage").document("uid-member").get().to_dict()["verified"] == 4
    too_many = "; ".join(f"x{i}@a.com" for i in range(30))
    assert client.post("/verify/single", headers=bearer("member"), json={"email": too_many}).status_code == 400


def test_bulk_verify_is_persisted_and_owned(client: TestClient, db: Any) -> None:
    csv = "store,Email\nA,a@shop.com\nB,b@bad.com\nC,A@shop.com\nD,c@catch.io\n"
    r = client.post("/verify/bulk", headers=bearer("member"), files={"file": ("list.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert r.json()["total"] == 3
    job = db.collection("email_verifications").document(job_id).get().to_dict()
    assert job["userId"] == "uid-member" and job["status"] == "done"
    assert len(list(db.collection("email_verifications").document(job_id).collection("results").stream())) == 3

    res = client.get(f"/results/{job_id}", headers=bearer("member")).json()
    assert res["status"] == "done" and res["percent"] == 100 and len(res["results"]) == 3
    assert client.get(f"/results/{job_id}", headers=bearer("member-2")).status_code == 403
    exp = client.get(f"/results/{job_id}/export?category=bounce", headers=bearer("member"))
    assert exp.text.splitlines() == ["email", "b@bad.com"]


def test_bulk_verify_rejects_bad_files(client: TestClient, db: Any) -> None:
    assert client.post("/verify/bulk", headers=bearer("member"), files={"file": ("x.txt", "a@b.com", "text/plain")}).status_code == 400
    assert client.post("/verify/bulk", headers=bearer("member"), files={"file": ("x.csv", "name\nnobody\n", "text/csv")}).status_code == 400


def test_failed_bulk_job_reads_as_not_found(client: TestClient, db: Any) -> None:
    job_id = store.create_doc(store.VERIFICATIONS, {"userId": "uid-member", "status": "failed", "progress": 0, "total": 3, "createdAt": store.now()})
    r = client.get(f"/results/{job_id}", headers=bearer("member"))
    assert r.status_code == 404 and "upload the file again" in r.json()["detail"]


def test_scout_log_feeds_dashboard(client: TestClient, db: Any) -> None:
    for i in (1, 2):
        client.post("/scout/log", headers=bearer("member"), json={"batch_number": i, "total_batches": 2, "email_count": 45, "subject": "Hello"})
    d = client.get("/dashboard/me", headers=bearer("member")).json()
    assert d["scouted"]["total"] == 90 and d["scouted"]["batches_total"] == 2 and d["scouted"]["today"] == 90
    assert d["recent_activity"][0]["batch_number"] == 2 and "user_id" not in d["recent_activity"][0]
    assert len(d["verify_series"]) == 30 and d["scout_series"][-1]["value"] == 90
    assert client.get("/dashboard/me", headers=bearer("member-2")).json()["scouted"]["total"] == 0


def test_admin_pool_management(client: TestClient, db: Any) -> None:
    csv = "email,website\nOne@Store.com,store.com\ntwo@store.com,\none@store.com,dup\n"
    r = client.post("/admin/leads/upload/ng?name=Nigeria%20shops", headers=bearer("admin"), files={"file": ("p.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json() == {"message": "Upload complete", "country": "NG", "inserted": 2, "skipped_duplicates": 1, "total_processed": 3}
    lead = docs(db, "scouting_leads", email="one@store.com")[0]
    assert lead["uploadedBy"] == "uid-admin" and lead["domain"] == "store.com"
    # Re-uploading the same file inserts nothing new.
    again = client.post("/admin/leads/upload/NG", headers=bearer("admin"), files={"file": ("p.csv", csv, "text/csv")}).json()
    assert again["inserted"] == 0 and again["skipped_duplicates"] == 3
    stats = client.get("/admin/leads/stats", headers=bearer("admin")).json()
    assert stats == [{"country": "NG", "name": "Nigeria shops", "flag": "", "total": 2, "available": 2, "claimed": 0, "expired": 0}]
    assert len(client.get("/admin/leads/sample/NG", headers=bearer("admin")).json()) == 2
    assert client.get("/admin/leads/jobs", headers=bearer("admin")).status_code == 200
    assert client.get("/dashboard/admin", headers=bearer("admin")).status_code == 200
    assert client.get("/dashboard/admin/user/uid-member", headers=bearer("admin")).json()["user"]["name"] == "Ada Obi"
    assert client.delete("/admin/leads/delete-available/NG", headers=bearer("admin")).json()["deleted"] == 2
    assert client.post("/admin/leads/upload/NG", headers=bearer("member"), files={"file": ("p.csv", csv, "text/csv")}).status_code == 403


def test_admin_dashboard_aggregates_platform_usage(client: TestClient, db: Any) -> None:
    client.post("/scout/log", headers=bearer("member"), json={"batch_number": 1, "total_batches": 1, "email_count": 40, "subject": "A"})
    client.post("/scout/log", headers=bearer("member-2"), json={"batch_number": 1, "total_batches": 1, "email_count": 10, "subject": "B"})
    client.post("/verify/single", headers=bearer("member"), json={"email": "x@shop.com"})
    d = client.get("/dashboard/admin", headers=bearer("admin")).json()
    assert d["scouted"]["total"] == 50 and d["verified"]["total"] == 1
    assert [t["user_id"] for t in d["top_users"]] == ["uid-member", "uid-member-2"]
    assert d["active_members_this_month"] == 1
    assert {a["user_id"] for a in d["recent_activity"]} == {"uid-member", "uid-member-2"}


def test_errors_never_leak_internals(client: TestClient, db: Any, monkeypatch: Any) -> None:
    import app.routes.dashboard as dash

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("google.api_core.exceptions.DeadlineExceeded: projects/eliteachievers-b8782")

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
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": "https://evil.example"}).headers
