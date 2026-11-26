"""Contracts, branded receipts, the handover code (OTP) and the public reputation profile."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.transaction import Milestone, Transaction
from app.routers import deal_documents as D
from app.routers import transactions as T
from app.routers import users as U
from app.schemas.transaction import FundMilestoneBody
from tests.test_deal_rules import avail, db, deal, deposit, mk, notes  # noqa: F401 — db is a fixture

REQ = SimpleNamespace(headers={"x-forwarded-for": "203.0.113.7"}, client=None)


def err(e):
    return e.value.detail["error"] if isinstance(e.value.detail, dict) else e.value.detail


def sign(db, tx, u, name=None):
    return D.sign_contract(tx.id, D.SignBody(full_name=name or f"{u.first_name} {u.last_name}"), REQ, u, db)


def test_contract_is_generated_from_the_terms_and_needs_both_signatures(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, _ = deal(db, b, s, 100, [("Design", 40), ("Build", 60)])
    c = D.get_contract(tx.id, b, db)["contract"]
    assert c["version"] == 1 and c["status"] == "awaiting_signatures"
    assert [m["title"] for m in c["terms"]["milestones"]] == ["Design", "Build"] and c["terms"]["amount"] == 100

    with pytest.raises(HTTPException) as e:
        sign(db, tx, b, name="Someone Else")
    assert err(e) == "NAME_MISMATCH"
    out = sign(db, tx, b)
    assert out["contract_signed"] is False and "Please sign the contract" in notes(db, s)
    with pytest.raises(HTTPException) as e:
        sign(db, tx, b)
    assert err(e) == "ALREADY_SIGNED"
    out = sign(db, tx, s)
    assert out["contract_signed"] is True and out["contract"]["status"] == "signed"
    assert out["contract"]["buyer_signed_name"] == "B X"

    page = D.contract_document(tx.id, s, db).body.decode()
    assert "Escrow Agreement" in page and "Signed by both parties" in page and "Design" in page


def test_changing_the_terms_makes_a_new_version_that_must_be_signed_again(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m1, _m2) = deal(db, b, s, 100, [("Design", 40), ("Build", 60)])
    sign(db, tx, b); sign(db, tx, s)
    db.get(Milestone, m1.id).title = "Design v2"; db.commit()
    c = D.get_contract(tx.id, b, db)
    assert c["contract"]["version"] == 2 and c["contract"]["status"] == "awaiting_signatures"
    assert c["contract_signed"] is False


def test_outsider_cannot_see_documents(db):
    b, s, x = mk(db, "buyer@x.com"), mk(db, "seller@x.com"), mk(db, "x@x.com")
    tx, _ = deal(db, b, s, 50)
    with pytest.raises(HTTPException) as e:
        D.get_contract(tx.id, x, db)
    assert e.value.status_code == 403
    with pytest.raises(HTTPException):
        D.receipt_document(tx.id, None, x, db)


def test_receipt_uses_the_buyers_logo_template_and_real_payments(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    b.business_name, b.business_logo_url, b.receipt_template = "Ade Stores", "https://cdn.example/logo.png", "modern"
    db.commit()
    tx, ms = deal(db, b, s, 50)
    deposit(db, b, "USD", 50)
    T.fund_deal(tx.id, FundMilestoneBody(), b, db)
    T._release_milestone_escrow(db, tx, ms[0]); db.commit()

    page = D.receipt_document(tx.id, None, s, db).body.decode()
    assert "https://cdn.example/logo.png" in page and "Ade Stores" in page and "#0f766e" in page   # modern
    assert "Paid into escrow" in page and "Released to seller" in page and "USD 50.00" in page
    minimal = D.receipt_document(tx.id, "minimal", b, db).body.decode()
    assert "#0f766e" not in minimal
    assert "sent to buyer@x.com" in D.email_receipt(tx.id, D.ReceiptEmailBody(template="classic"), b, db)["message"]


def test_handover_code_releases_the_milestone_only_with_the_right_code(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, ms = deal(db, b, s, 50)
    m = ms[0]
    with pytest.raises(HTTPException) as e:
        D.create_handover_code(tx.id, m.id, b, db)                     # not paid yet
    assert err(e) == "NOT_READY"
    deposit(db, b, "USD", 50)
    T.fund_deal(tx.id, FundMilestoneBody(), b, db)
    with pytest.raises(HTTPException) as e:
        D.create_handover_code(tx.id, m.id, s, db)                     # only the buyer gets the code
    assert e.value.status_code == 403
    code = D.create_handover_code(tx.id, m.id, b, db)["code"]
    assert len(code) == 6
    assert T.milestone_to_public_dict(db.get(Milestone, m.id))["handover_code_active"] is True

    with pytest.raises(HTTPException) as e:
        D.confirm_handover(tx.id, m.id, D.HandoverConfirmBody(code=code), b, db)   # buyer can't self-confirm
    assert e.value.status_code == 403
    wrong = "000000" if code != "000000" else "111111"
    with pytest.raises(HTTPException) as e:
        D.confirm_handover(tx.id, m.id, D.HandoverConfirmBody(code=wrong), s, db)
    assert err(e) == "WRONG_CODE" and "4 attempts left" in e.value.detail["message"]
    assert avail(db, s, "USD")[0] == Decimal("0")

    out = D.confirm_handover(tx.id, m.id, D.HandoverConfirmBody(code=code), s, db)
    assert out["released"] is True and avail(db, s, "USD")[0] == Decimal("50")
    mm = db.get(Milestone, m.id)
    assert mm.status == "completed" and mm.handover_confirmed_at is not None and mm.handover_code_hash is None
    assert "Handover confirmed" in notes(db, b)
    # Replaying the same code can't pay twice.
    assert D.confirm_handover(tx.id, m.id, D.HandoverConfirmBody(code=code), s, db)["released"] is True
    assert avail(db, s, "USD")[0] == Decimal("50")


def test_too_many_wrong_codes_locks_the_code(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, ms = deal(db, b, s, 20)
    deposit(db, b, "USD", 20)
    T.fund_deal(tx.id, FundMilestoneBody(), b, db)
    code = D.create_handover_code(tx.id, ms[0].id, b, db)["code"]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(D.HANDOVER_MAX_ATTEMPTS):
        with pytest.raises(HTTPException):
            D.confirm_handover(tx.id, ms[0].id, D.HandoverConfirmBody(code=wrong), s, db)
    with pytest.raises(HTTPException) as e:
        D.confirm_handover(tx.id, ms[0].id, D.HandoverConfirmBody(code=code), s, db)
    assert err(e) == "TOO_MANY_ATTEMPTS"


def test_public_profile_separates_facts_ratings_and_disputes_and_hides_contact_details(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, ms = deal(db, b, s, 50)
    deposit(db, b, "USD", 50)
    T.fund_deal(tx.id, FundMilestoneBody(), b, db)
    T._release_milestone_escrow(db, tx, ms[0]); db.commit()
    p = U.public_profile(s.id, db)
    assert p["verified"]["completed_deals"] == 1 and p["verified"]["completed_as_seller"] == 1
    assert p["verified"]["completed_value_usd"] == 50.0 and p["verified"]["identity_verified"] is True
    assert p["ratings"]["as_seller"]["count"] == 0 and p["disputes"]["total"] == 0
    assert "email" not in p and "seller@x.com" not in str(p) and p["display_name"] == "S X."
