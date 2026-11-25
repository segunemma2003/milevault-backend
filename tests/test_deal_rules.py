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
