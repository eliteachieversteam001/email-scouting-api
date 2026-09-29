"""Firestore persistence layer (real Firestore emulator)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app import store


def test_insert_leads_is_idempotent_per_pool(db: Any) -> None:
    assert store.insert_leads("US", [("a@x.com", ""), ("b@x.com", "x.com"), ("a@x.com", "")], "uid-admin") == (2, 1)
    assert store.insert_leads("US", [("a@x.com", ""), ("c@x.com", "")], "uid-admin") == (1, 1)
    # The same email may exist in a different pool (MailClean's unique key was email+country).
    assert store.insert_leads("GB", [("a@x.com", "")], "uid-admin") == (1, 0)
    assert store.pool_counts("US") == {"total": 3, "available": 3, "claimed": 0, "expired": 0}
    snap = db.collection(store.LEADS).document(store.lead_id("US", "b@x.com")).get().to_dict()
    assert snap["domain"] == "x.com" and snap["status"] == "available" and snap["claimedBy"] is None


def test_large_upload_spans_batches(db: Any) -> None:
    rows = [(f"u{i}@x.com", "") for i in range(1000)]
    assert store.insert_leads("BIG", rows, "uid-admin") == (1000, 0)
    assert store.available_count("BIG") == 1000
    assert store.delete_leads("BIG", available_only=False) == 1000
    assert store.pool_counts("BIG")["total"] == 0


def test_concurrent_claims_never_double_claim(db: Any) -> None:
    store.insert_leads("US", [(f"l{i}@x.com", "") for i in range(250)], "uid-admin")

    def claim(uid: str) -> list[str]:
        return store.claim_leads("US", 150, uid, f"job{uid}")

    with ThreadPoolExecutor(max_workers=2) as ex:
        a, b = ex.map(claim, ["A", "B"])
    assert not set(a) & set(b), "a lead was claimed by two members"
    assert len(a) + len(b) == 250
    assert store.available_count("US") == 0
    for uid, emails in (("A", a), ("B", b)):
        for e in emails[:5]:
            d = db.collection(store.LEADS).document(store.lead_id("US", e)).get().to_dict()
            assert d["claimedBy"] == uid and d["claimedByJob"] == f"job{uid}"


def test_release_only_unverified_leads(db: Any) -> None:
    store.insert_leads("US", [(f"l{i}@x.com", "") for i in range(10)], "uid-admin")
    claimed = store.claim_leads("US", 10, "uid-m", "job1")
    store.mark_lead_verified("US", claimed[0], "job1", {"category": "delivers"})
    store.mark_lead_verified("US", claimed[1], "other-job", {"category": "delivers"})  # ignored: not this job's lead
    store.release_unverified_leads("job1")
    assert store.pool_counts("US") == {"total": 10, "available": 9, "claimed": 1, "expired": 0}


def test_results_round_trip_in_order(db: Any) -> None:
    job = store.create_doc(store.JOBS, {"userId": "u"})
    rows = [{"email": f"r{i}@x.com"} for i in range(1234)]
    store.write_results(store.JOBS, job, 1, rows[:600])
    store.write_results(store.JOBS, job, 601, rows[600:])
    assert store.read_results(store.JOBS, job) == rows


def test_usage_increments_are_atomic_and_scoped(db: Any) -> None:
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda _: store.record_verification("uid-a", "A", 5, "single"), range(20)))
    store.record_outreach("uid-a", "A", 1, 3, 45, "Hi")
    store.record_outreach("uid-b", "B", 1, 1, 10, "Yo")
    a, b, platform = store.usage("uid-a"), store.usage("uid-b"), store.usage(store.PLATFORM)
    assert a["verified"] == {"today": 100, "week": 100, "month": 100, "total": 100}
    assert a["scouted"]["total"] == 45 and a["batches"]["total"] == 1
    assert b["verified"]["total"] == 0 and b["scouted"]["total"] == 10
    assert platform["verified"]["total"] == 100 and platform["scouted"]["total"] == 55
    assert a["verify_series"][-1]["value"] == 100 and len(a["verify_series"]) == 30
    assert [r["subject"] for r in store.recent_outreach("uid-a", 10)] == ["Hi"]
    assert len(store.recent_outreach(None, 10)) == 2


def test_claimed_today_tracks_daily_usage(db: Any) -> None:
    assert store.claimed_today("uid-a") == 0
    store.record_claim("uid-a", 300)
    store.record_claim("uid-a", 200)
    assert store.claimed_today("uid-a") == 500
    assert store.claimed_today("uid-b") == 0


def test_every_member_record_carries_the_uid(db: Any) -> None:
    store.record_verification("uid-z", "Z", 1, "single")
    store.record_outreach("uid-z", "Z", 1, 1, 1, "s")
    for d in db.collection(store.HISTORY).stream():
        assert d.to_dict()["userId"] == "uid-z"
    assert db.collection(store.USAGE).document("uid-z").get().to_dict()["userId"] == "uid-z"
