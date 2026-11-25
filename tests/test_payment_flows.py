"""Deposit/withdrawal flows with Paystack and Quidax mocked."""
import os
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite://")

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
import app.models.agent, app.models.currency, app.models.crypto, app.models.content, app.models.payments  # noqa
from app.config import settings
from app.database import Base
from app.models.payments import PayoutDestination, ProviderCustomer, VirtualAccount
from app.models.user import User
from app.models.wallet import WalletBalance, WalletTransaction
from app.services import ledger, paystack_rails, payment_flows, quidax
from app.services.payment_flows import FlowError


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _c(dbapi_conn, _r):
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _b(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "QUIDAX_SECRET_KEY", "qx_test")
    monkeypatch.setattr(settings, "NGN_PAYMENT_RAIL", "paystack")  # PayHub tests opt in explicitly
    monkeypatch.setattr(payment_flows, "create_notification", lambda *a, **k: None)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def user(db, first="Ada", last="Obi", kyc=True):
    u = User(id=str(uuid.uuid4()), first_name=first, last_name=last, email=f"{uuid.uuid4().hex[:8]}@x.com",
             hashed_password="x", role="buyer", is_kyc_verified=kyc, is_active=True)
    db.add(u)
    db.commit()
    return u


def avail(db, u, cur):
    db.expire_all()
    b = db.query(WalletBalance).filter_by(user_id=u.id, currency=cur).first()
    return (Decimal(str(b.amount or 0)), Decimal(str(b.pending_amount or 0))) if b else (Decimal(0), Decimal(0))


# ─── Paystack deposits ────────────────────────────────────────────────────────

def test_paystack_charge_credits_once_using_verified_amount(db, monkeypatch):
    u = user(db)
    db.add(VirtualAccount(user_id=u.id, provider="paystack", currency="NGN", status="active",
                          provider_customer_code="CUS_1", account_number="0123456789"))
    db.commit()
    monkeypatch.setattr(paystack_rails, "verify_transaction", lambda ref: {
        "status": "success", "amount": Decimal("15000.50"), "currency": "NGN", "reference": ref,
        "channel": "dedicated_nuban", "customer_code": "CUS_1", "customer_email": None, "metadata": {}})
    # webhook body claims a different amount — ignored, the API's answer is used
    hint = {"amount": 999999999, "authorization": {"sender_name": "OBI ADA CHIOMA"}}
    assert payment_flows.handle_paystack_charge(db, "REF1", hint=hint) == "credited"
    assert payment_flows.handle_paystack_charge(db, "REF1", hint=hint) == "duplicate"
    assert avail(db, u, "NGN")[0] == Decimal("15000.50")
    assert db.query(WalletTransaction).filter_by(type="deposit").one().flagged_high_risk is False


def test_paystack_dva_from_someone_else_is_flagged(db, monkeypatch):
    u = user(db)
    db.add(VirtualAccount(user_id=u.id, provider="paystack", currency="NGN", status="active", provider_customer_code="CUS_2"))
    db.commit()
    monkeypatch.setattr(paystack_rails, "verify_transaction", lambda ref: {
        "status": "success", "amount": Decimal("500"), "currency": "NGN", "reference": ref,
        "channel": "dedicated_nuban", "customer_code": "CUS_2", "customer_email": None, "metadata": {}})
    payment_flows.handle_paystack_charge(db, "REF2", hint={"authorization": {"sender_name": "JOHN STRANGER"}})
    assert db.query(WalletTransaction).filter_by(reference="REF2").one().flagged_high_risk is True


def test_paystack_failed_charge_credits_nothing(db, monkeypatch):
    user(db)
    monkeypatch.setattr(paystack_rails, "verify_transaction", lambda ref: {"status": "failed"})
    assert payment_flows.handle_paystack_charge(db, "REF3").startswith("not_successful")


# ─── Quidax deposits ──────────────────────────────────────────────────────────

def test_quidax_deposit_credits_once_and_sweeps_once(db, monkeypatch):
    u = user(db)
    db.add(ProviderCustomer(user_id=u.id, provider="quidax", provider_customer_id="QX1"))
    db.commit()
    monkeypatch.setattr(quidax, "fetch_deposit", lambda **k: {
        "id": "D1", "currency": "USDT", "amount": Decimal("25.123456"), "status": "accepted",
        "txid": "abc", "network": "trc20", "address": "T..."})
    sweeps = []
    monkeypatch.setattr(quidax, "sweep_to_master", lambda **k: sweeps.append(k))
    assert payment_flows.handle_quidax_deposit(db, "QX1", "D1") == "credited"
    assert payment_flows.handle_quidax_deposit(db, "QX1", "D1") == "duplicate"
    assert avail(db, u, "USDT")[0] == Decimal("25.123456")
    assert len(sweeps) == 1


def test_quidax_unconfirmed_deposit_waits(db, monkeypatch):
    u = user(db)
    db.add(ProviderCustomer(user_id=u.id, provider="quidax", provider_customer_id="QX2"))
    db.commit()
    monkeypatch.setattr(quidax, "fetch_deposit", lambda **k: {
        "id": "D2", "currency": "USDT", "amount": Decimal("5"), "status": "submitted", "txid": None,
        "network": "trc20", "address": None})
    assert payment_flows.handle_quidax_deposit(db, "QX2", "D2").startswith("not_final")
    assert avail(db, u, "USDT")[0] == Decimal(0)


# ─── Destinations ─────────────────────────────────────────────────────────────

def test_bank_destination_requires_matching_name(db, monkeypatch):
    u = user(db, "Ada", "Obi")
    monkeypatch.setattr(paystack_rails, "list_banks", lambda: [{"code": "058", "name": "GTBank"}])
    monkeypatch.setattr(paystack_rails, "resolve_account", lambda **k: "STRANGER JOHN")
    with pytest.raises(FlowError) as e:
        payment_flows.add_bank_destination(db, u, bank_code="058", account_number="0123456789", label=None)
    assert e.value.code == "NAME_MISMATCH"
    monkeypatch.setattr(paystack_rails, "resolve_account", lambda **k: "OBI ADA CHIOMA")
    dest, token = payment_flows.add_bank_destination(db, u, bank_code="058", account_number="0123456789", label="Main")
    assert dest.status == "pending_confirmation" and dest.bank_name == "GTBank" and token


def test_crypto_destination_validation(db):
    u = user(db)
    with pytest.raises(FlowError) as e:
        payment_flows.add_crypto_destination(db, u, currency="USDT", network="trc20", address="0xabc",
                                             destination_tag=None, label=None)
    assert e.value.code == "INVALID_ADDRESS"
    with pytest.raises(FlowError) as e:
        payment_flows.add_crypto_destination(db, u, currency="DOGE", network="doge", address="D" * 34,
                                             destination_tag=None, label=None)
    assert e.value.code == "UNSUPPORTED_RAIL"


def test_unverified_user_cannot_add_destination(db):
    u = user(db, kyc=False)
    with pytest.raises(FlowError) as e:
        payment_flows.add_crypto_destination(db, u, currency="USDT", network="trc20",
                                             address="T" + "A" * 33, destination_tag=None, label=None)
    assert e.value.code == "KYC_REQUIRED"


def confirmed_crypto_dest(db, u, usable=True):
    dest, token = payment_flows.add_crypto_destination(db, u, currency="USDT", network="trc20",
                                                       address="T" + "B" * 33, destination_tag=None, label=None)
    db.commit()
    payment_flows.confirm_destination(db, token)
    if usable:
        dest.usable_from = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
    return dest


def test_confirm_starts_cooldown_and_token_is_single_use(db):
    u = user(db)
    dest, token = payment_flows.add_crypto_destination(db, u, currency="USDT", network="trc20",
                                                       address="T" + "C" * 33, destination_tag=None, label=None)
    db.commit()
    payment_flows.confirm_destination(db, token)
    assert dest.status == "confirmed" and not dest.is_usable
    assert u.withdrawal_cooldown_until and u.withdrawal_cooldown_until > datetime.utcnow()
    with pytest.raises(FlowError):
        payment_flows.confirm_destination(db, token)


# ─── Withdrawals & payouts ────────────────────────────────────────────────────

def test_withdraw_blocked_during_cooldown(db):
    u = user(db)
    ledger.credit(db, user_id=u.id, currency="USDT", amount="50", reference_type="t")
    db.commit()
    dest = confirmed_crypto_dest(db, u, usable=False)
    with pytest.raises(FlowError) as e:
        payment_flows.create_withdrawal(db, u, destination_id=dest.id, amount=10)
    assert e.value.code == "DESTINATION_COOLDOWN"


def test_crypto_withdrawal_full_cycle_and_exactly_once_settlement(db, monkeypatch):
    u = user(db)
    ledger.credit(db, user_id=u.id, currency="USDT", amount="50", reference_type="t")
    db.commit()
    dest = confirmed_crypto_dest(db, u)
    txn = payment_flows.create_withdrawal(db, u, destination_id=dest.id, amount="20.5")
    assert txn.status == "pending"                       # no auto limit → admin approval
    assert avail(db, u, "USDT") == (Decimal("29.5"), Decimal("20.5"))

    sent = []
    monkeypatch.setattr(quidax, "create_withdrawal", lambda **k: sent.append(k) or {"id": "W1", "status": "submitted"})
    assert payment_flows.dispatch_payout(db, txn) == "processing"
    assert payment_flows.dispatch_payout(db, txn).startswith("skipped")   # can't send twice
    assert len(sent) == 1 and sent[0]["reference"] == str(txn.id) and sent[0]["from_user"] == "me"

    monkeypatch.setattr(quidax, "fetch_withdrawal_by_reference", lambda **k: {"id": "W1", "status": "done"})
    assert payment_flows.handle_quidax_withdrawal(db, str(txn.id)) == "completed"
    assert payment_flows.handle_quidax_withdrawal(db, str(txn.id)) == "already_completed"
    assert avail(db, u, "USDT") == (Decimal("29.5"), Decimal("0"))


def test_failed_bank_payout_returns_funds(db, monkeypatch):
    u = user(db)
    ledger.credit(db, user_id=u.id, currency="NGN", amount="10000", reference_type="t")
    db.commit()
    monkeypatch.setattr(paystack_rails, "list_banks", lambda: [])
    monkeypatch.setattr(paystack_rails, "resolve_account", lambda **k: "OBI ADA")
    dest, token = payment_flows.add_bank_destination(db, u, bank_code="058", account_number="0123456789", label=None)
    db.commit()
    payment_flows.confirm_destination(db, token)
    dest.usable_from = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    txn = payment_flows.create_withdrawal(db, u, destination_id=dest.id, amount=4000)
    monkeypatch.setattr(paystack_rails, "create_recipient", lambda **k: "RCP_1")
    monkeypatch.setattr(paystack_rails, "initiate_transfer", lambda **k: {"transfer_code": "TRF_1", "status": "pending"})
    payment_flows.dispatch_payout(db, txn)
    monkeypatch.setattr(paystack_rails, "verify_transfer", lambda ref: {"status": "reversed"})
    assert payment_flows.handle_paystack_transfer(db, str(txn.id)) == "failed"
    assert avail(db, u, "NGN") == (Decimal("10000"), Decimal("0"))


def test_payout_timeout_is_not_retried_blindly(db, monkeypatch):
    u = user(db)
    ledger.credit(db, user_id=u.id, currency="USDT", amount="10", reference_type="t")
    db.commit()
    dest = confirmed_crypto_dest(db, u)
    txn = payment_flows.create_withdrawal(db, u, destination_id=dest.id, amount=5)

    def boom(**k):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(quidax, "create_withdrawal", boom)
    assert payment_flows.dispatch_payout(db, txn) == "processing_unknown"
    # poller later learns the real outcome by reference
    monkeypatch.setattr(quidax, "fetch_withdrawal_by_reference", lambda **k: {"id": "x", "status": "done"})
    assert payment_flows.poll_processing_payouts(db)["settled"] == 1
    assert avail(db, u, "USDT") == (Decimal("5"), Decimal("0"))


def test_every_withdrawal_waits_for_admin_approval(db, monkeypatch):
    u = user(db)
    ledger.credit(db, user_id=u.id, currency="USDT", amount="300", reference_type="t")
    db.commit()
    dest = confirmed_crypto_dest(db, u)
    monkeypatch.setattr(quidax, "create_withdrawal", lambda **k: {"id": "W9", "status": "submitted"})
    small = payment_flows.create_withdrawal(db, u, destination_id=dest.id, amount=100)
    big = payment_flows.create_withdrawal(db, u, destination_id=dest.id, amount=150)
    db.expire_all()
    assert db.get(WalletTransaction, small.id).status == "pending"  # even small ones: nothing is sent
    assert db.get(WalletTransaction, big.id).status == "pending"    # until an admin approves



# ─── PayHub rail (central gateway) ────────────────────────────────────────────

from app.services import payhub  # noqa: E402


@pytest.fixture()
def on_payhub(monkeypatch):
    monkeypatch.setattr(settings, "NGN_PAYMENT_RAIL", "payhub")
    monkeypatch.setattr(settings, "PAYHUB_API_KEY", "sk_test_bondrova")
    monkeypatch.setattr(settings, "PAYHUB_PASS_FEE_TO_PAYER", True)
    monkeypatch.setattr(payhub, "list_banks", lambda: [{"code": "058", "name": "GTBank"}])


def test_payhub_deposit_credits_full_amount_once(db, on_payhub, monkeypatch):
    u = user(db)
    monkeypatch.setattr(payhub, "fee_quote", lambda amt: {"amount": Decimal("10000"), "fee": Decimal("101.01"), "total": Decimal("10101.01")})
    made = {}
    monkeypatch.setattr(payhub, "create_charge", lambda **k: made.update(k) or {
        "reference": "phb_1", "checkout_url": "https://checkout/phb_1", "status": "pending", "amount": k["amount"], "currency": "NGN"})
    out = payment_flows.start_naira_deposit(db, u, 10000)
    assert out["checkout_url"] and made["amount"] == Decimal("10101.01")          # payer covers the fee
    assert made["metadata"]["user_id"] == u.id and "/dashboard/payouts" in made["redirect_url"]

    monkeypatch.setattr(payhub, "get_charge", lambda ref: {"reference": ref, "status": "success",
                                                         "amount": Decimal("10101.01"), "currency": "NGN"})
    assert payment_flows.handle_payhub_charge(db, "phb_1") == "credited"
    assert payment_flows.handle_payhub_charge(db, "phb_1") == "duplicate"         # webhook + return page
    assert avail(db, u, "NGN")[0] == Decimal("10000")                              # full amount credited


def test_payhub_underpaid_or_failed_charge_credits_nothing(db, on_payhub, monkeypatch):
    u = user(db)
    monkeypatch.setattr(payhub, "fee_quote", lambda amt: {"amount": Decimal(amt), "fee": Decimal("0"), "total": Decimal(amt)})
    monkeypatch.setattr(payhub, "create_charge", lambda **k: {"reference": "phb_2", "checkout_url": "x", "status": "pending",
                                                              "amount": k["amount"], "currency": "NGN"})
    payment_flows.start_naira_deposit(db, u, 5000)
    monkeypatch.setattr(payhub, "get_charge", lambda ref: {"reference": ref, "status": "success", "amount": Decimal("10"), "currency": "NGN"})
    assert payment_flows.handle_payhub_charge(db, "phb_2") == "amount_mismatch"
    assert avail(db, u, "NGN")[0] == Decimal(0)


def test_payhub_bank_payout_cycle(db, on_payhub, monkeypatch):
    u = user(db, "Ada", "Obi")
    ledger.credit(db, user_id=u.id, currency="NGN", amount="20000", reference_type="t")
    db.commit()
    monkeypatch.setattr(payhub, "register_bank_account", lambda **k: {"id": "ba_1", "status": "verified",
                                                                      "account_name": "OBI ADA C", "error": None})
    dest, token = payment_flows.add_bank_destination(db, u, bank_code="058", account_number="0123456789", label=None)
    assert dest.provider_recipient_code == "ba_1" and dest.bank_name == "GTBank"
    db.commit()
    payment_flows.confirm_destination(db, token)
    dest.usable_from = datetime.utcnow() - timedelta(minutes=1)
    db.commit()

    txn = payment_flows.create_withdrawal(db, u, destination_id=dest.id, amount=7500)
    sent = []
    monkeypatch.setattr(payhub, "request_payout", lambda **k: sent.append(k) or {"id": "po_1", "status": "processing", "failure_reason": None})
    assert payment_flows.dispatch_payout(db, txn) == "processing"
    assert sent[0]["client_reference"] == str(txn.id) and sent[0]["bank_account_id"] == "ba_1"

    monkeypatch.setattr(payhub, "get_payout", lambda pid: {"id": pid, "status": "completed", "client_reference": str(txn.id),
                                                          "failure_reason": None, "amount": Decimal("7500")})
    assert payment_flows.handle_payhub_payout(db, "po_1") == "completed"
    assert payment_flows.handle_payhub_payout(db, "po_1") == "already_completed"
    assert avail(db, u, "NGN") == (Decimal("12500"), Decimal("0"))


def test_payhub_rejected_payout_returns_funds(db, on_payhub, monkeypatch):
    u = user(db)
    ledger.credit(db, user_id=u.id, currency="NGN", amount="5000", reference_type="t")
    db.commit()
    monkeypatch.setattr(payhub, "register_bank_account", lambda **k: {"id": "ba_2", "status": "verified", "account_name": "OBI ADA", "error": None})
    dest, token = payment_flows.add_bank_destination(db, u, bank_code="058", account_number="0123456789", label=None)
    db.commit()
    payment_flows.confirm_destination(db, token)
    dest.usable_from = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    txn = payment_flows.create_withdrawal(db, u, destination_id=dest.id, amount=5000)
    monkeypatch.setattr(payhub, "request_payout", lambda **k: {"id": "po_2", "status": "failed", "failure_reason": "Insufficient float"})
    assert payment_flows.dispatch_payout(db, txn) == "failed"
    assert avail(db, u, "NGN") == (Decimal("5000"), Decimal("0"))


def test_payhub_bank_name_mismatch_rejected(db, on_payhub, monkeypatch):
    u = user(db, "Ada", "Obi")
    monkeypatch.setattr(payhub, "register_bank_account", lambda **k: {"id": "ba_3", "status": "verified", "account_name": "JOHN STRANGER", "error": None})
    with pytest.raises(FlowError) as e:
        payment_flows.add_bank_destination(db, u, bank_code="058", account_number="0123456789", label=None)
    assert e.value.code == "NAME_MISMATCH"


def test_payhub_webhook_signature(monkeypatch):
    import hashlib, hmac, time as _t
    monkeypatch.setattr(settings, "PAYHUB_WEBHOOK_SECRET", "whsec_test")
    body = b'{"type":"charge.succeeded"}'
    t = int(_t.time())
    sig = hmac.new(b"whsec_test", f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    assert payhub.verify_webhook(body, f"t={t},v1={sig}")
    assert not payhub.verify_webhook(body, f"t={t},v1={'0' * 64}")
    assert not payhub.verify_webhook(body, f"t={t - 3600},v1={sig}")  # stale


def test_withdrawal_fee_is_deducted_from_the_payout(db, monkeypatch):
    from app.models.currency import PlatformSettings
    from app.models.wallet import LedgerEntry
    db.add(PlatformSettings(id="default", withdrawal_fee_percent=1, withdrawal_fee_fixed=0, fee_currency="USDT"))
    db.commit()
    u = user(db)
    ledger.credit(db, user_id=u.id, currency="USDT", amount="200", reference_type="t")
    db.commit()
    dest = confirmed_crypto_dest(db, u)
    sent = {}
    monkeypatch.setattr(quidax, "create_withdrawal", lambda **k: sent.update(k) or {"id": "W1", "status": "submitted"})
    txn = payment_flows.create_withdrawal(db, u, destination_id=dest.id, amount=100)
    assert txn.fee == 1.0 and avail(db, u, "USDT") == (Decimal("100"), Decimal("100"))
    payment_flows.dispatch_payout(db, txn)
    assert float(sent["amount"]) == 99.0                         # user receives amount − fee
    payment_flows.settle_payout(db, str(txn.id), success=True)
    assert avail(db, u, "USDT") == (Decimal("100"), Decimal("0"))
    fee_rows = db.query(LedgerEntry).filter_by(credit_account="platform_fees").all()
    assert sum(Decimal(str(e.amount)) for e in fee_rows) == Decimal("1")
