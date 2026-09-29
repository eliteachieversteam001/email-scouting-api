"""MailClean deliverability classification (logic unchanged)."""

from __future__ import annotations

from app.common import emails_from_csv
from app.verifier import EmailVerifier


def test_classification_buckets() -> None:
    v = EmailVerifier.__new__(EmailVerifier)
    assert v.classify(False, True, True, False, False) == ("bounce", "HIGH")
    assert v.classify(True, False, None, False, False) == ("bounce", "HIGH")
    assert v.classify(True, True, False, False, False) == ("bounce", "HIGH")
    assert v.classify(True, True, True, True, False) == ("bounce", "HIGH")
    assert v.classify(True, True, True, False, True) == ("unknown", "MEDIUM")
    assert v.classify(True, True, True, False, False) == ("delivers", "LOW")
    assert v.classify(True, True, None, False, False) == ("delivers", "LOW")


def test_disposable_list_loads_from_package() -> None:
    assert "mailinator.com" in EmailVerifier().disposable_domains


def test_invalid_format_short_circuits() -> None:
    r = EmailVerifier().verify("not-an-email")
    assert r["category"] == "bounce" and r["format_valid"] is False and r["message"] == "Invalid email format"


def test_csv_column_detection_matches_mailclean() -> None:
    assert emails_from_csv(b"name,Contact Email\nA,a@x.com\nB,A@X.com\nC,b@y.org\n") == ["a@x.com", "b@y.org"]
    assert emails_from_csv(b"c1,c2\nfoo,x@y.com\nbar,z@y.com\n") == ["x@y.com", "z@y.com"]
