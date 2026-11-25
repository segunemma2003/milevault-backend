"""
Deal rules: USD base, single-payment deals, pay per milestone or all at once, no start until a
milestone is 100% paid, pause after a release when the next milestone isn't paid, pay from naira.
"""
import os
import uuid
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import app.models  # noqa
import app.models.agent, app.models.currency, app.models.crypto, app.models.content, app.models.payments, app.models.support  # noqa
from app.database import Base
from app.models.currency import Currency, ExchangeRate
from app.models.notification import Notification
from app.models.transaction import Milestone, Transaction
from app.models.user import User
from app.models.wallet import WalletBalance
from app.routers import transactions as T
from app.schemas.transaction import FundMilestoneBody, MilestoneCreate, TransactionCreate
from app.services import ledger


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _no_autobegin(dbapi_conn, _rec):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    usd = Currency(code="USD", name="US Dollar", symbol="$", decimal_places=2)
    ngn = Currency(code="NGN", name="Naira", symbol="₦", decimal_places=2)
    s.add_all([usd, ngn]); s.flush()
    s.add(ExchangeRate(from_currency_id=usd.id, to_currency_id=ngn.id, rate=1500.0, spread_percent=0.0))
    from app.models.currency import PlatformSettings
    # Fee-free unless a test sets fees (the model's defaults charge 2.5%).
    s.add(PlatformSettings(id="default", escrow_fee_percent=0, min_fee_amount=0, withdrawal_fee_percent=0,
                           withdrawal_fee_fixed=0, dispute_fee_fixed=0, dispute_fee_percent=0))
    s.commit()
    yield s
    s.close()


def mk(db, email):
    u = User(id=str(uuid.uuid4()), first_name=email[0].upper(), last_name="X", email=email, hashed_password="x",
             role="buyer", is_kyc_verified=True, is_active=True, is_email_verified=True)
    db.add(u); db.commit()
    return u


def deposit(db, u, cur, amt):
    ledger.credit(db, user_id=u.id, currency=cur, amount=amt, reference_type="test_deposit"); db.commit()


def avail(db, u, cur):
    db.expire_all()
    b = db.query(WalletBalance).filter_by(user_id=u.id, currency=cur).first()
    return (Decimal(str(b.amount)), Decimal(str(b.escrow_amount))) if b else (Decimal(0), Decimal(0))


def deal(db, buyer, seller, amount, milestones=None, currency="USD"):
    ms = [MilestoneCreate(title=t, amount=a) for t, a in (milestones or [])]
    out = T.create_transaction(TransactionCreate(title="Logo", amount=amount, currency=currency,
                                                 counterparty_email=seller.email, milestones=ms), buyer, db)
    tid = out["id"] if isinstance(out, dict) else out.id
    T.accept_transaction(tid, seller, db)
    tx = db.get(Transaction, tid)
    return tx, T._ordered_milestones(db, tid)


def notes(db, u):
    return [n.title for n in db.query(Notification).filter_by(user_id=u.id).all()]


def test_deal_without_milestones_is_one_full_payment(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, ms = deal(db, b, s, 20)
    assert tx.currency == "USD" and tx.type == "one_time"
    assert len(ms) == 1 and ms[0].title == "Full payment" and ms[0].amount == 20
    deposit(db, b, "USD", 20)
    T.fund_deal(tx.id, FundMilestoneBody(), b, db)
    T._release_milestone_escrow(db, tx, ms[0]); db.commit()
    db.refresh(tx)
    assert tx.status == "completed" and avail(db, s, "USD")[0] == Decimal("20")


def test_deal_entered_in_naira_is_stored_in_usd(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, ms = deal(db, b, s, 30000, currency="NGN")   # ₦30,000 at 1,500 = $20
    assert tx.currency == "USD" and tx.amount == 20.0 and ms[0].amount == 20.0 and ms[0].currency == "USD"


def test_milestones_must_add_up(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    with pytest.raises(HTTPException) as e:
        deal(db, b, s, 100, [("A", 30), ("B", 30)])
    assert e.value.detail["error"] == "MILESTONES_DONT_ADD_UP"


def test_partial_payment_that_covers_no_milestone_cannot_start(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m1, m2) = deal(db, b, s, 100, [("Design", 40), ("Build", 60)])
    deposit(db, b, "USD", 30)
    out = T.fund_deal(tx.id, FundMilestoneBody(amount=30), b, db)
    assert out["fully_funded"] == [] and "can't start" in out["message"]
    with pytest.raises(HTTPException):
        T.start_milestone(tx.id, m1.id, s, db)
    with pytest.raises(HTTPException) as e:
        T.request_start(tx.id, b, db)
    assert e.value.detail["error"] == "NOTHING_TO_START"
    assert "Partial payment received" in notes(db, s)


def test_pay_all_at_once_but_release_is_per_milestone_and_buyer_can_nudge(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m1, m2) = deal(db, b, s, 100, [("Design", 40), ("Build", 60)])
    deposit(db, b, "USD", 100)
    out = T.fund_deal(tx.id, FundMilestoneBody(), b, db)
    assert set(out["fully_funded"]) == {m1.id, m2.id} and avail(db, b, "USD") == (Decimal("0"), Decimal("100"))
    assert T.request_start(tx.id, b, db)["message"]
    assert "Buyer asked you to start" in notes(db, s)
    T.start_milestone(tx.id, m1.id, s, db)
    T._release_milestone_escrow(db, tx, db.get(Milestone, m1.id)); db.commit()
    db.refresh(tx)
    assert avail(db, s, "USD")[0] == Decimal("40") and avail(db, b, "USD")[1] == Decimal("60")  # only m1 released
    assert tx.status != "awaiting_funding"   # m2 is paid — no pause


def test_deal_pauses_after_release_until_next_milestone_is_paid(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m1, m2) = deal(db, b, s, 100, [("Design", 40), ("Build", 60)])
    deposit(db, b, "USD", 100)
    T.fund_milestone(tx.id, m1.id, FundMilestoneBody(), b, db)
    T._release_milestone_escrow(db, tx, db.get(Milestone, m1.id)); db.commit()
    db.refresh(tx)
    assert tx.status == "awaiting_funding"
    assert "Deal paused — fund the next milestone" in notes(db, b) and "Deal paused" in notes(db, s)
    T.fund_milestone(tx.id, m2.id, FundMilestoneBody(amount=20), b, db)   # still partial → still paused
    db.refresh(tx); assert tx.status == "awaiting_funding"
    T.fund_milestone(tx.id, m2.id, FundMilestoneBody(), b, db)             # rest → resumes
    db.refresh(tx); assert tx.status == "active"
    assert "Deal resumed — milestone funded" in notes(db, s)


def test_buyer_can_pay_from_naira_balance(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m1,) = deal(db, b, s, 20)
    deposit(db, b, "NGN", 50000)
    out = T.fund_deal(tx.id, FundMilestoneBody(pay_from="NGN"), b, db)
    assert out["converted"]["from"] == "NGN" and out["converted"]["amount"] == 30000.0  # $20 × 1,500
    assert avail(db, b, "NGN")[0] == Decimal("20000") and avail(db, b, "USD") == (Decimal("0"), Decimal("20"))


def test_paying_from_naira_without_enough_naira_fails_cleanly(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, _ = deal(db, b, s, 20)
    deposit(db, b, "NGN", 1000)
    with pytest.raises(HTTPException) as e:
        T.fund_deal(tx.id, FundMilestoneBody(pay_from="NGN"), b, db)
    assert e.value.detail["error"] == "INSUFFICIENT_FUNDS"
    assert avail(db, b, "NGN")[0] == Decimal("1000")   # nothing taken


# ─── Fees (set by admin / finance) ────────────────────────────────────────────

from app.models.currency import PlatformSettings  # noqa: E402
from app.models.wallet import LedgerEntry  # noqa: E402


def set_fees(db, **kw):
    s = db.query(PlatformSettings).filter_by(id="default").first() or PlatformSettings(id="default")
    defaults = dict(escrow_fee_percent=0, min_fee_amount=0, max_fee_amount=None, fee_paid_by="buyer",
                    buyer_fee_share=100, seller_fee_share=0, withdrawal_fee_percent=0, withdrawal_fee_fixed=0,
                    dispute_fee_fixed=0, dispute_fee_percent=0, fee_currency="USD")
    for k, v in {**defaults, **kw}.items():
        setattr(s, k, v)
    db.add(s); db.commit()


def platform_revenue(db, cur="USD"):
    return sum(Decimal(str(e.amount)) for e in db.query(LedgerEntry).filter_by(currency=cur, credit_account="platform_fees").all())


def test_buyer_pays_the_escrow_fee_on_top(db):
    set_fees(db, escrow_fee_percent=5, fee_paid_by="buyer")
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m1, m2) = deal(db, b, s, 100, [("Design", 40), ("Build", 60)])
    assert (m1.buyer_fee, m1.seller_fee, m2.buyer_fee) == (2.0, 0.0, 3.0)
    deposit(db, b, "USD", 105)
    T.fund_deal(tx.id, FundMilestoneBody(), b, db)        # pays 100 + 5 fee
    assert avail(db, b, "USD") == (Decimal("0"), Decimal("105"))
    T._release_milestone_escrow(db, tx, db.get(Milestone, m1.id)); db.commit()
    assert avail(db, s, "USD")[0] == Decimal("40")          # seller gets the full price
    assert avail(db, b, "USD")[1] == Decimal("63")          # 60 + 3 still escrowed for m2
    assert platform_revenue(db) == Decimal("2")


def test_seller_pays_the_escrow_fee_from_the_release(db):
    set_fees(db, escrow_fee_percent=10, fee_paid_by="seller")
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m,) = deal(db, b, s, 50)
    deposit(db, b, "USD", 50)
    T.fund_deal(tx.id, FundMilestoneBody(), b, db)
    T._release_milestone_escrow(db, tx, db.get(Milestone, m.id)); db.commit()
    assert avail(db, s, "USD")[0] == Decimal("45") and platform_revenue(db) == Decimal("5")


def test_split_fee_and_min_max(db):
    set_fees(db, escrow_fee_percent=1, min_fee_amount=4, fee_paid_by="split", buyer_fee_share=50, seller_fee_share=50)
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m,) = deal(db, b, s, 100)                         # 1% = 1 → raised to the 4 minimum
    assert (m.buyer_fee, m.seller_fee) == (2.0, 2.0)
    set_fees(db, escrow_fee_percent=10, max_fee_amount=3, fee_paid_by="buyer")
    tx2, (m2,) = deal(db, b, s, 100)
    assert m2.buyer_fee == 3.0                             # capped at the 3 maximum
    assert m.buyer_fee == 2.0                              # earlier deal keeps its locked fee


def test_refund_returns_the_buyers_fee(db):
    """The buyer's fee share sits in escrow with the milestone, so refunding funded_amount returns it."""
    set_fees(db, escrow_fee_percent=5, fee_paid_by="buyer")
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m,) = deal(db, b, s, 100)
    deposit(db, b, "USD", 105)
    T.fund_deal(tx.id, FundMilestoneBody(), b, db)
    db.refresh(m)
    assert m.funded_amount == 105.0 and m.is_funded


def test_ongoing_milestone_is_never_paused(db):
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m1, m2, m3) = deal(db, b, s, 90, [("A", 30), ("B", 30), ("C", 30)])
    deposit(db, b, "USD", 60)
    T.fund_milestone(tx.id, m1.id, FundMilestoneBody(), b, db)
    T.fund_milestone(tx.id, m2.id, FundMilestoneBody(), b, db)
    T.start_milestone(tx.id, m2.id, s, db)
    T._release_milestone_escrow(db, tx, db.get(Milestone, m1.id)); db.commit()
    db.refresh(tx)
    assert tx.status != "awaiting_funding"                 # B is being worked on → no pause


def test_dispute_fee_is_paid_by_whoever_opens_it(db):
    from app.routers import disputes
    from app.schemas.dispute import DisputeCreate
    set_fees(db, dispute_fee_fixed=5, dispute_fee_percent=0)
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m,) = deal(db, b, s, 20)
    body = dict(transaction_id=tx.id, milestone_id=m.id, title="Not delivered", reason="non_delivery",
                description="The seller never delivered the agreed work at all.", evidence_urls=["https://example.com/proof.png"])
    with pytest.raises(HTTPException) as e:
        disputes.create_dispute(DisputeCreate(**body), s, db)   # seller has nothing
    assert e.value.detail["error"] == "DISPUTE_FEE_UNPAID"
    deposit(db, s, "NGN", 10000)                                 # only naira: charged ₦7,500 at 1,500
    disputes.create_dispute(DisputeCreate(**body), s, db)
    assert avail(db, s, "NGN")[0] == Decimal("2500") and platform_revenue(db, "NGN") == Decimal("7500")


def test_dashboard_stats_show_escrow_and_incoming(db):
    from app.routers.dashboard import get_dashboard_stats
    b, s = mk(db, "buyer@x.com"), mk(db, "seller@x.com")
    tx, (m1, m2) = deal(db, b, s, 100, [("Design", 40), ("Build", 60)])
    deposit(db, b, "USD", 150)
    T.fund_milestone(tx.id, m1.id, FundMilestoneBody(), b, db)
    sb = get_dashboard_stats(b, db)
    usd = next(x for x in sb["wallet_balance"] if x["currency"] == "USD")
    assert (usd["amount"], usd["escrow"]) == (110.0, 40.0) and sb["active_transactions"] == 1
    ss = get_dashboard_stats(s, db)
    assert ss["incoming_escrow"] == [{"currency": "USD", "amount": 40.0}] and ss["active_transactions"] == 1
