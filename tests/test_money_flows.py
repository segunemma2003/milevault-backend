"""
End-to-end money flows through the real endpoint functions (called directly with a session),
so the rewired code paths — not just the ledger service — are exercised.
"""
import os
import uuid
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
import app.models.agent, app.models.currency, app.models.crypto, app.models.content  # noqa: F401,E401
from app.database import Base
from app.models.transaction import Milestone, Transaction
from app.models.user import User
from app.models.wallet import LedgerEntry, WalletBalance, WalletTransaction
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
    yield s
    s.close()


def user(db, **kw):
    u = User(id=str(uuid.uuid4()), first_name="A", last_name="B", email=f"{uuid.uuid4().hex[:8]}@x.com",
             hashed_password="x", role="buyer", is_kyc_verified=True, is_active=True, **kw)
    db.add(u)
    db.commit()
    return u


def fund(db, u, currency, amount):
    ledger.credit(db, user_id=u.id, currency=currency, amount=amount, reference_type="test_deposit")
    db.commit()


def bal(db, u, currency):
    db.expire_all()
    b = db.query(WalletBalance).filter_by(user_id=u.id, currency=currency).first()
    if not b:
        return (Decimal(0), Decimal(0), Decimal(0))
    return tuple(Decimal(str(x or 0)) for x in (b.amount, b.escrow_amount, b.pending_amount))


# ─── Wallet: transfer ─────────────────────────────────────────────────────────

def test_transfer_moves_exact_amount_and_links_both_legs(db):
    from app.routers.wallet import transfer_funds
    from app.schemas.wallet import TransferRequest
    a, b = user(db), user(db)
    fund(db, a, "USDT", "10")
    out = transfer_funds(TransferRequest(recipient_email=b.email, amount=3.333333, currency="USDT"), a, db)
    assert bal(db, a, "USDT")[0] == Decimal("6.666667")
    assert bal(db, b, "USDT")[0] == Decimal("3.333333")
    assert Decimal(str(out["sender_new_balance"])) == Decimal("6.666667")
    legs = db.query(WalletTransaction).filter(WalletTransaction.type.in_(("transfer_out", "transfer_in"))).all()
    assert len(legs) == 2 and legs[0].reference and legs[0].reference == legs[1].reference


def test_transfer_insufficient_and_frozen(db):
    from app.routers.wallet import transfer_funds
    from app.schemas.wallet import TransferRequest
    a, b = user(db), user(db)
    fund(db, a, "NGN", "100")
    with pytest.raises(HTTPException) as e:
        transfer_funds(TransferRequest(recipient_email=b.email, amount=100.01, currency="NGN"), a, db)
    assert e.value.status_code == 422
    assert bal(db, a, "NGN")[0] == Decimal("100")
    a.wallet_frozen = True
    db.commit()
    with pytest.raises(HTTPException) as e:
        transfer_funds(TransferRequest(recipient_email=b.email, amount=1, currency="NGN"), a, db)
    assert e.value.status_code == 403


def test_admin_reversal_takes_money_back_from_recipient(db):
    from app.routers.admin import reverse_transfer
    from app.routers.wallet import transfer_funds
    from app.schemas.wallet import TransferRequest
    a, b, admin = user(db), user(db), user(db, is_admin=True)
    fund(db, a, "NGN", "500")
    transfer_funds(TransferRequest(recipient_email=b.email, amount=200, currency="NGN"), a, db)
    out_id = db.query(WalletTransaction).filter_by(type="transfer_out").one().id
    reverse_transfer(out_id, "test", db, admin)
    assert bal(db, a, "NGN")[0] == Decimal("500")
    assert bal(db, b, "NGN")[0] == Decimal("0")  # previously recipient kept it → money created
    with pytest.raises(HTTPException):
        reverse_transfer(out_id, "again", db, admin)  # no longer completed → can't reverse twice


# ─── Wallet: withdrawals ─────────────────────────────────────────────────────

def test_withdraw_then_reject_restores_balance(db):
    from app.routers.admin import reject_withdrawal
    from app.routers.wallet import withdraw
    from app.schemas.wallet import WithdrawRequest
    u, admin = user(db), user(db, is_admin=True)
    fund(db, u, "USDT", "50.123456")
    r = withdraw(WithdrawRequest(amount=50.123456, currency="USDT", method="crypto"), u, db)
    assert bal(db, u, "USDT") == (Decimal("0"), Decimal("0"), Decimal("50.123456"))
    reject_withdrawal(r["wallet_transaction_id"], "test", db, admin)
    assert bal(db, u, "USDT") == (Decimal("50.123456"), Decimal("0"), Decimal("0"))
    with pytest.raises(HTTPException) as e:  # can't reject (or approve) twice
        reject_withdrawal(r["wallet_transaction_id"], "again", db, admin)
    assert e.value.status_code == 409


def test_withdraw_then_approve_pays_out_once(db):
    from app.routers.admin import approve_withdrawal
    from app.routers.wallet import withdraw
    from app.schemas.wallet import WithdrawRequest
    u, admin = user(db), user(db, is_admin=True)
    fund(db, u, "NGN", "1000")
    r = withdraw(WithdrawRequest(amount=400, currency="NGN"), u, db)
    approve_withdrawal(r["wallet_transaction_id"], None, db, admin)
    assert bal(db, u, "NGN") == (Decimal("600"), Decimal("0"), Decimal("0"))
    with pytest.raises(HTTPException):
        approve_withdrawal(r["wallet_transaction_id"], None, db, admin)
    assert bal(db, u, "NGN") == (Decimal("600"), Decimal("0"), Decimal("0"))


def test_withdraw_more_than_balance_is_refused(db):
    from app.routers.wallet import withdraw
    from app.schemas.wallet import WithdrawRequest
    u = user(db)
    fund(db, u, "NGN", "10")
    with pytest.raises(HTTPException) as e:
        withdraw(WithdrawRequest(amount=10.01, currency="NGN"), u, db)
    assert e.value.status_code == 422
    assert bal(db, u, "NGN") == (Decimal("10"), Decimal("0"), Decimal("0"))
    assert db.query(WalletTransaction).filter_by(type="withdrawal").count() == 0


# ─── Escrow: fund → release ───────────────────────────────────────────────────

def make_deal(db, buyer, seller, currency="USDT", amount=100.0):
    tx = Transaction(id=str(uuid.uuid4()), title="Deal", amount=amount, currency=currency, type="milestone",
                     status="approved", buyer_id=buyer.id, seller_id=seller.id)
    ms = Milestone(id=str(uuid.uuid4()), transaction_id=tx.id, title="M1", amount=amount, currency=currency,
                   status="pending", funded_amount=0.0, is_funded=False)
    db.add_all([tx, ms])
    db.commit()
    return tx, ms


def test_fund_and_release_exactly_once(db):
    from app.routers.transactions import _release_milestone_escrow, fund_milestone
    from app.schemas.transaction import FundMilestoneBody
    buyer, seller = user(db), user(db)
    fund(db, buyer, "USDT", "150")
    tx, ms = make_deal(db, buyer, seller)
    fund_milestone(tx.id, ms.id, FundMilestoneBody(), buyer, db)
    assert bal(db, buyer, "USDT") == (Decimal("50"), Decimal("100"), Decimal("0"))

    tx = db.get(Transaction, tx.id)
    ms = db.get(Milestone, ms.id)
    _release_milestone_escrow(db, tx, ms, None)
    db.commit()
    # second release (double click / auto-release race) is a no-op
    assert _release_milestone_escrow(db, db.get(Transaction, tx.id), db.get(Milestone, ms.id), None) is False
    db.commit()
    assert bal(db, buyer, "USDT") == (Decimal("50"), Decimal("0"), Decimal("0"))
    assert bal(db, seller, "USDT")[0] == Decimal("100")
    assert db.query(LedgerEntry).filter_by(reference_type="milestone_release").count() == 1


def test_fund_insufficient_balance(db):
    from app.routers.transactions import fund_milestone
    from app.schemas.transaction import FundMilestoneBody
    buyer, seller = user(db), user(db)
    fund(db, buyer, "USDT", "99.999999")
    tx, ms = make_deal(db, buyer, seller)
    with pytest.raises(HTTPException) as e:
        fund_milestone(tx.id, ms.id, FundMilestoneBody(), buyer, db)
    assert e.value.status_code == 400
    assert bal(db, buyer, "USDT") == (Decimal("99.999999"), Decimal("0"), Decimal("0"))


def test_deadline_cancel_only_refunds_this_deal(db):
    from app.services.funding_deadline_enforcement import enforce_funding_deadline_if_due  # noqa: F401
    from app.routers.transactions import fund_milestone
    from app.schemas.transaction import FundMilestoneBody
    buyer, seller = user(db), user(db)
    fund(db, buyer, "USDT", "300")
    tx1, ms1 = make_deal(db, buyer, seller, amount=100.0)
    tx2, ms2 = make_deal(db, buyer, seller, amount=100.0)
    fund_milestone(tx1.id, ms1.id, FundMilestoneBody(amount=40), buyer, db)  # partial on deal 1
    fund_milestone(tx2.id, ms2.id, FundMilestoneBody(), buyer, db)            # full on deal 2
    assert bal(db, buyer, "USDT") == (Decimal("160"), Decimal("140"), Decimal("0"))

    # cancel deal 1 the way the deadline task does
    ledger.move(db, user_id=buyer.id, currency="USDT", amount=40, from_bucket="escrow", to_bucket="available",
                reference_type="funding_deadline_refund", idempotency_key=tx1.id)
    assert ledger.move(db, user_id=buyer.id, currency="USDT", amount=40, from_bucket="escrow",
                       to_bucket="available", reference_type="funding_deadline_refund",
                       idempotency_key=tx1.id) is False
    db.commit()
    # deal 2's 100 stays in escrow (the old code moved ALL escrow back to available)
    assert bal(db, buyer, "USDT") == (Decimal("200"), Decimal("100"), Decimal("0"))
