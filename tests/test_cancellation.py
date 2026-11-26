"""
Mutual cancellation: once a deal has started neither party can cancel it alone. One asks, the other
accepts (deal cancelled, unreleased escrow back to the buyer) or declines (deal carries on).
"""
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.dispute import Dispute
from app.models.transaction import Milestone, Transaction
from app.routers import transactions as T
from app.schemas.transaction import FundMilestoneBody, TransactionUpdate
from tests.test_deal_rules import avail, db, deal, deposit, mk, notes  # noqa: F401 — db is a fixture

ask = lambda db, tx, u, reason="Scope changed, can't continue": T.request_cancellation(  # noqa: E731
    tx.id, T.CancelRequestBody(reason=reason), u, db)
accept = lambda db, tx, u: T.accept_cancellation(tx.id, T.CancelResponseBody(), u, db)  # noqa: E731
reject = lambda db, tx, u, note=None: T.reject_cancellation(tx.id, T.CancelResponseBody(note=note), u, db)  # noqa: E731


def err(e):
    return e.value.detail["error"] if isinstance(e.value.detail, dict) else e.value.detail


def test_started_deal_cannot_be_cancelled_alone(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, _ = deal(db, b, s, 100)
    with pytest.raises(HTTPException) as e:
        T.cancel_transaction(tx.id, b, db)
    assert e.value.status_code == 409 and err(e) == "CANCEL_REQUIRES_AGREEMENT"
    with pytest.raises(HTTPException) as e:
        T.update_transaction(tx.id, TransactionUpdate(status="cancelled"), s, db)
    assert err(e) == "STATUS_NOT_EDITABLE"
    db.refresh(tx)
    assert tx.status == "funding_in_progress"


def test_invitation_not_yet_accepted_can_still_be_withdrawn(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    from app.schemas.transaction import TransactionCreate
    out = T.create_transaction(TransactionCreate(title="Logo", amount=50, currency="USD",
                                                 counterparty_email=s.email, milestones=[]), b, db)
    tid = out["id"] if isinstance(out, dict) else out.id
    with pytest.raises(HTTPException) as e:
        ask(db, db.get(Transaction, tid), b)
    assert err(e) == "NOT_STARTED"
    T.cancel_transaction(tid, b, db)
    assert db.get(Transaction, tid).status == "cancelled"


def test_accept_refunds_unreleased_escrow_and_keeps_released_milestones_paid(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    other_seller = mk(db, "other@x.com")
    tx, (m1, m2) = deal(db, b, s, 100, [("Design", 40), ("Build", 60)])
    other, _ = deal(db, b, other_seller, 25)          # buyer's escrow for ANOTHER deal must be untouched
    deposit(db, b, "USD", 125)
    T.fund_deal(tx.id, FundMilestoneBody(), b, db)
    T.fund_deal(other.id, FundMilestoneBody(), b, db)
    T.start_milestone(tx.id, m1.id, s, db)
    T._release_milestone_escrow(db, tx, db.get(Milestone, m1.id)); db.commit()
    assert avail(db, b, "USD") == (Decimal("0"), Decimal("85"))   # 60 for m2 + 25 other deal

    ask(db, tx, s)
    assert "Cancellation requested" in notes(db, b)
    with pytest.raises(HTTPException) as e:
        accept(db, tx, s)                               # requester can't accept their own request
    assert err(e) == "OWN_REQUEST"

    out = accept(db, tx, b)
    assert out["refunded"] == {"USD": 60.0}
    db.refresh(tx)
    assert tx.status == "cancelled"
    assert avail(db, b, "USD") == (Decimal("60"), Decimal("25"))  # other deal's escrow still locked
    assert avail(db, s, "USD")[0] == Decimal("40")                # released milestone stays paid
    assert db.get(Milestone, m1.id).status == "completed"
    m2r = db.get(Milestone, m2.id)
    assert m2r.status == "cancelled" and m2r.funded_amount == 0 and not m2r.is_funded
    assert "Deal cancelled" in notes(db, s) and "Deal cancelled — refund issued" in notes(db, b)
    assert T.transaction_to_dict(tx)["cancellation_request"]["status"] == "accepted"


def test_reject_keeps_deal_running_and_allows_a_new_request(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, _ = deal(db, b, s, 50)
    deposit(db, b, "USD", 50)
    T.fund_deal(tx.id, FundMilestoneBody(), b, db)
    ask(db, tx, b)
    with pytest.raises(HTTPException) as e:
        ask(db, tx, s)
    assert err(e) == "REQUEST_PENDING"
    reject(db, tx, s, note="Almost done, let me finish")
    db.refresh(tx)
    assert tx.status == "active" and avail(db, b, "USD") == (Decimal("0"), Decimal("50"))
    assert "Cancellation declined" in notes(db, b)
    ask(db, tx, b, reason="Asking again after a call")   # a new request is allowed after a rejection
    T.withdraw_cancellation(tx.id, b, db)
    assert T.transaction_to_dict(db.get(Transaction, tx.id))["cancellation_request"]["status"] == "withdrawn"


def test_outsiders_and_disputed_deals_are_blocked(db):
    b, s, x = mk(db, "buyer@x.com"), mk(db, "seller@x.com"), mk(db, "x@x.com")
    tx, _ = deal(db, b, s, 50)
    with pytest.raises(HTTPException) as e:
        ask(db, tx, x)
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        ask(db, tx, b, reason="no")
    assert err(e) == "REASON_REQUIRED"
    ask(db, tx, b)
    db.add(Dispute(transaction_id=tx.id, raised_by=b.id, title="Bad work", description="x", reason="quality", status="open"))
    db.commit()
    with pytest.raises(HTTPException) as e:
        accept(db, tx, s)
    assert err(e) == "DEAL_DISPUTED"


def test_started_deal_cannot_be_ended_by_declining_it(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, _ = deal(db, b, s, 50)
    deposit(db, b, "USD", 50)
    T.fund_deal(tx.id, FundMilestoneBody(), b, db)
    with pytest.raises(HTTPException) as e:
        T.decline_transaction(tx.id, None, s, db)
    assert err(e) == "CANCEL_REQUIRES_AGREEMENT"
    db.refresh(tx)
    assert tx.status == "active" and avail(db, b, "USD") == (Decimal("0"), Decimal("50"))
