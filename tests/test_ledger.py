"""Unit tests for the central money service (app/services/ledger.py)."""
import os
import uuid
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (registers all tables)
import app.models.agent, app.models.currency, app.models.crypto, app.models.content  # noqa: F401,E401
from app.database import Base
from app.models.user import User
from app.models.wallet import LedgerEntry, ProcessedEvent, WalletBalance
from app.services import ledger
from app.services.ledger import InsufficientFunds, LedgerError


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")

    # pysqlite quirk: without this, SAVEPOINTs commit early and rollback() can't undo them.
    # PostgreSQL (production) behaves correctly without any of this.
    @event.listens_for(engine, "connect")
    def _no_autobegin(dbapi_conn, _rec):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def make_user(db, email=None):
    u = User(
        id=str(uuid.uuid4()), first_name="T", last_name="U",
        email=email or f"{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", role="buyer",
    )
    db.add(u)
    db.flush()
    return u.id


def bal(db, user_id, currency):
    b = db.query(WalletBalance).filter_by(user_id=user_id, currency=currency).first()
    return (Decimal(str(b.amount)), Decimal(str(b.escrow_amount or 0)), Decimal(str(b.pending_amount or 0)))


# ─── Precision ────────────────────────────────────────────────────────────────

def test_quantize_per_currency(db):
    assert ledger.quantize("10.005", "NGN") == Decimal("10.01")          # kobo, half-up
    assert ledger.quantize("1.23456789", "USDT") == Decimal("1.234568")  # 6 dp
    assert ledger.quantize("0.123456789", "BTC") == Decimal("0.12345679")  # 8 dp (satoshi)


def test_float_inputs_do_not_drift(db):
    u = make_user(db)
    for _ in range(10):
        ledger.credit(db, user_id=u, currency="NGN", amount=0.1, reference_type="deposit")
    assert bal(db, u, "NGN")[0] == Decimal("1.00")  # 10 × 0.1 == 1.00 exactly, not 0.9999999


# ─── Exactly-once ─────────────────────────────────────────────────────────────

def test_duplicate_deposit_event_credits_once(db):
    u = make_user(db)
    first = ledger.credit(db, user_id=u, currency="USDT", amount="50", reference_type="deposit:quidax",
                          idempotency_key="tx-abc")
    second = ledger.credit(db, user_id=u, currency="USDT", amount="50", reference_type="deposit:quidax",
                           idempotency_key="tx-abc")
    db.commit()
    assert (first, second) == (True, False)
    assert bal(db, u, "USDT")[0] == Decimal("50")
    assert db.query(LedgerEntry).count() == 1
    assert db.query(ProcessedEvent).count() == 1


def test_same_event_id_different_source_is_independent(db):
    u = make_user(db)
    assert ledger.credit(db, user_id=u, currency="NGN", amount=1, reference_type="deposit:paystack", idempotency_key="R1")
    assert ledger.credit(db, user_id=u, currency="NGN", amount=1, reference_type="deposit:quidax", idempotency_key="R1")
    assert bal(db, u, "NGN")[0] == Decimal("2")


def test_claim_event_survives_in_open_transaction(db):
    assert ledger.claim_event(db, "x", "1") is True
    assert ledger.claim_event(db, "x", "1") is False
    # session still usable after the caught IntegrityError
    assert ledger.claim_event(db, "x", "2") is True


# ─── Overdraft protection ─────────────────────────────────────────────────────

def test_cannot_overdraw(db):
    u = make_user(db)
    ledger.credit(db, user_id=u, currency="NGN", amount="100", reference_type="deposit")
    with pytest.raises(InsufficientFunds):
        ledger.move(db, user_id=u, currency="NGN", amount="100.01",
                    from_bucket="available", to_bucket="escrow", reference_type="milestone_fund")
    db.rollback()


def test_failed_move_does_not_consume_idempotency_key(db):
    u = make_user(db)
    ledger.credit(db, user_id=u, currency="NGN", amount="10", reference_type="deposit")
    db.commit()
    with pytest.raises(InsufficientFunds):
        ledger.move(db, user_id=u, currency="NGN", amount="20", from_bucket="available", to_bucket="escrow",
                    reference_type="milestone_fund", idempotency_key="ms-1")
    db.rollback()
    # retry after topping up succeeds with the SAME key
    ledger.credit(db, user_id=u, currency="NGN", amount="10", reference_type="deposit")
    assert ledger.move(db, user_id=u, currency="NGN", amount="20", from_bucket="available", to_bucket="escrow",
                       reference_type="milestone_fund", idempotency_key="ms-1") is True


def test_rejects_non_positive_and_unknown_bucket(db):
    u = make_user(db)
    with pytest.raises(LedgerError):
        ledger.credit(db, user_id=u, currency="NGN", amount=0, reference_type="deposit")
    with pytest.raises(LedgerError):
        ledger.credit(db, user_id=u, currency="NGN", amount="0.001", reference_type="deposit")  # rounds to 0.00
    with pytest.raises(LedgerError):
        ledger.credit(db, user_id=u, currency="NGN", amount=5, to_bucket="savings", reference_type="deposit")


# ─── Full escrow cycle ────────────────────────────────────────────────────────

def test_escrow_cycle_conserves_money(db):
    buyer, seller = make_user(db), make_user(db)
    ledger.credit(db, user_id=buyer, currency="USDT", amount="100", reference_type="deposit:quidax", idempotency_key="d1")
    ledger.move(db, user_id=buyer, currency="USDT", amount="60", from_bucket="available", to_bucket="escrow",
                reference_type="milestone_fund", idempotency_key="m1")
    ledger.transfer(db, currency="USDT", amount="60", from_user_id=buyer, from_bucket="escrow",
                    to_user_id=seller, to_bucket="available", reference_type="milestone_release", idempotency_key="m1")
    # double release attempt is a no-op
    assert ledger.transfer(db, currency="USDT", amount="60", from_user_id=buyer, from_bucket="escrow",
                           to_user_id=seller, to_bucket="available", reference_type="milestone_release",
                           idempotency_key="m1") is False
    ledger.move(db, user_id=seller, currency="USDT", amount="60", from_bucket="available", to_bucket="pending",
                reference_type="withdrawal_request", idempotency_key="w1")
    ledger.debit(db, user_id=seller, currency="USDT", amount="60", from_bucket="pending",
                 reference_type="withdrawal_paid", idempotency_key="w1")
    db.commit()
    assert bal(db, buyer, "USDT") == (Decimal("40"), Decimal("0"), Decimal("0"))
    assert bal(db, seller, "USDT") == (Decimal("0"), Decimal("0"), Decimal("0"))
    # 100 in, 60 out → 40 still held for the buyer
    ext_in = sum(Decimal(str(e.amount)) for e in db.query(LedgerEntry).filter_by(debit_account="external"))
    ext_out = sum(Decimal(str(e.amount)) for e in db.query(LedgerEntry).filter_by(credit_account="external"))
    assert ext_in - ext_out == Decimal("40")


def test_fee_split_adds_up_to_the_kobo(db):
    buyer, seller, platform = make_user(db), make_user(db), make_user(db)
    ledger.credit(db, user_id=buyer, currency="NGN", amount="10000.33", reference_type="deposit")
    ledger.move(db, user_id=buyer, currency="NGN", amount="10000.33", from_bucket="available", to_bucket="escrow",
                reference_type="milestone_fund")
    fee = ledger.quantize(Decimal("10000.33") * Decimal("0.015"), "NGN")  # 150.00495 → 150.00
    net = Decimal("10000.33") - fee
    ledger.transfer(db, currency="NGN", amount=net, from_user_id=buyer, from_bucket="escrow",
                    to_user_id=seller, to_bucket="available", reference_type="milestone_release")
    ledger.transfer(db, currency="NGN", amount=fee, from_user_id=buyer, from_bucket="escrow",
                    to_user_id=platform, to_bucket="available", reference_type="platform_fee")
    assert bal(db, buyer, "NGN")[1] == Decimal("0")
    assert bal(db, seller, "NGN")[0] + bal(db, platform, "NGN")[0] == Decimal("10000.33")
    assert bal(db, seller, "NGN")[0] == Decimal("9850.33")


# ─── Reconciliation ───────────────────────────────────────────────────────────

def test_reconciliation_clean_then_detects_tampering(db):
    from app.services.reconciliation import compute_mismatches, negative_balances
    buyer, seller = make_user(db), make_user(db)
    ledger.credit(db, user_id=buyer, currency="USDT", amount="100", reference_type="deposit")
    ledger.move(db, user_id=buyer, currency="USDT", amount="30", from_bucket="available", to_bucket="escrow",
                reference_type="milestone_fund")
    ledger.transfer(db, currency="USDT", amount="30", from_user_id=buyer, from_bucket="escrow",
                    to_user_id=seller, to_bucket="available", reference_type="milestone_release")
    db.commit()
    assert compute_mismatches(db) == []
    assert negative_balances(db) == []

    # someone edits a balance directly (no ledger entry) → caught
    b = db.query(WalletBalance).filter_by(user_id=seller, currency="USDT").one()
    b.amount = 31.0
    db.commit()
    mm = compute_mismatches(db)
    assert len(mm) == 1 and mm[0]["user_id"] == seller and Decimal(mm[0]["difference"]) == Decimal("1")
