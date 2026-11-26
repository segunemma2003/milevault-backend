"""
Deposit → wallet → withdrawal flows for Paystack (NGN) and Quidax (crypto).

Rules enforced here:
  * Only KYC-verified users get deposit rails or can withdraw.
  * Webhooks are hints: every event is re-fetched from the provider before money moves.
  * Every credit/payout settlement is exactly-once (ledger idempotency keys).
  * Payouts only go to a confirmed PayoutDestination whose cooldown has passed.
  * A withdrawal is marked completed/failed ONLY from the provider's confirmed status.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.payments import PayoutDestination, ProviderCustomer, UserDepositAddress, VirtualAccount
from app.models.user import User
from app.models.wallet import WalletTransaction
from app.services import ledger, payhub, paystack_rails, quidax
from app.services.notification_service import create_notification

logger = logging.getLogger("bondrova.payments")


class FlowError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ─── helpers ──────────────────────────────────────────────────────────────────

def _require_kyc(user: User) -> None:
    if not user.is_kyc_verified:
        raise FlowError("KYC_REQUIRED", "Complete identity verification (KYC) to use this feature.", 403)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _name_tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", (s or "").lower()) if len(t) > 1}


def names_match(kyc_first: str, kyc_last: str, bank_name: str) -> bool:
    """Bank names are often 'SURNAME FIRSTNAME MIDDLE' — require both first and last name, any order."""
    bank = _name_tokens(bank_name)
    first, last = _name_tokens(kyc_first), _name_tokens(kyc_last)
    return bool(first) and bool(last) and first <= bank and last <= bank


ADDRESS_PATTERNS = {
    "trc20": re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),
    "bep20": re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "erc20": re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "btc": re.compile(r"^(bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$"),
}


def valid_address(network: str, address: str) -> bool:
    pat = ADDRESS_PATTERNS.get((network or "").lower())
    return bool(pat and pat.match((address or "").strip()))


def ngn_rail() -> str:
    """Which gateway carries naira: "payhub" (central gateway) or "paystack" (direct)."""
    return (settings.NGN_PAYMENT_RAIL or "payhub").strip().lower()


def ngn_enabled() -> bool:
    return payhub.enabled() if ngn_rail() == "payhub" else paystack_rails.enabled()


def ngn_banks() -> list[dict]:
    try:
        return payhub.list_banks() if ngn_rail() == "payhub" else paystack_rails.list_banks()
    except Exception:
        return []


def _provider_customer(db: Session, user_id: str, provider: str) -> Optional[ProviderCustomer]:
    return db.query(ProviderCustomer).filter_by(user_id=user_id, provider=provider).first()


# ═════════════════════════════════════════════════════════════════════════════
#  DEPOSIT RAILS
# ═════════════════════════════════════════════════════════════════════════════

def ensure_virtual_account(db: Session, user: User) -> VirtualAccount:
    """Get or create the user's Paystack dedicated NGN account."""
    _require_kyc(user)
    va = db.query(VirtualAccount).filter_by(user_id=user.id, provider="paystack", currency="NGN").first()
    if va and va.status == "active":
        return va
    if not paystack_rails.enabled():
        raise FlowError("PROVIDER_NOT_CONFIGURED", "Bank deposits are not available yet.", 503)
    if not va:
        va = VirtualAccount(user_id=user.id, provider="paystack", currency="NGN", status="pending")
        db.add(va)
        db.flush()
    try:
        if not va.provider_customer_code:
            va.provider_customer_code = paystack_rails.create_customer(
                email=user.email, first_name=user.first_name, last_name=user.last_name, phone=user.phone,
            )
        acct = paystack_rails.create_dedicated_account(customer_code=va.provider_customer_code)
        va.provider_account_id = acct["id"]
        va.account_number = acct["account_number"]
        va.account_name = acct["account_name"]
        va.bank_name = acct["bank_name"]
        va.status = "active" if va.account_number else "pending"
        va.failure_reason = None
    except (paystack_rails.PaystackError, httpx.HTTPError) as exc:
        va.status = "failed"
        va.failure_reason = str(exc)[:500]
        db.commit()
        raise FlowError("VIRTUAL_ACCOUNT_FAILED", f"Couldn't create your bank deposit account: {exc}", 502)
    db.commit()
    return va


def _quidax_user_id(db: Session, user: User) -> str:
    pc = _provider_customer(db, user.id, "quidax")
    if pc:
        return pc.provider_customer_id
    qid = quidax.create_sub_account(email=user.email, first_name=user.first_name,
                                    last_name=user.last_name, phone=user.phone)
    db.add(ProviderCustomer(user_id=user.id, provider="quidax", provider_customer_id=qid))
    db.flush()
    return qid


def ensure_deposit_address(db: Session, user: User, currency: str, network: str) -> UserDepositAddress:
    _require_kyc(user)
    currency, network = currency.upper(), network.lower()
    if (currency, network) not in quidax.rails():
        raise FlowError("UNSUPPORTED_RAIL", f"{currency} on {network.upper()} isn't supported.", 422)
    row = db.query(UserDepositAddress).filter_by(user_id=user.id, currency=currency, network=network).first()
    if row and row.status == "active" and row.address:
        return row
    if not quidax.enabled():
        raise FlowError("PROVIDER_NOT_CONFIGURED", "Crypto deposits are not available yet.", 503)
    if not row:
        row = UserDepositAddress(user_id=user.id, provider="quidax", currency=currency, network=network)
        db.add(row)
        db.flush()
    try:
        qid = _quidax_user_id(db, user)
        addr = quidax.create_deposit_address(quidax_user_id=qid, currency=currency, network=network)
        if not addr.get("address"):  # some networks generate asynchronously
            addr = quidax.fetch_deposit_address(quidax_user_id=qid, currency=currency, network=network)
        row.address = addr.get("address")
        row.destination_tag = addr.get("destination_tag")
        row.status = "active" if row.address else "pending"
        row.failure_reason = None
    except (quidax.QuidaxError, httpx.HTTPError) as exc:
        row.status = "failed"
        row.failure_reason = str(exc)[:500]
        db.commit()
        raise FlowError("DEPOSIT_ADDRESS_FAILED", f"Couldn't create your {currency} address: {exc}", 502)
    db.commit()
    return row


MIN_NGN_DEPOSIT = Decimal("100")


def start_naira_deposit(db: Session, user: User, amount) -> dict:
    """PayHub checkout: card or bank transfer. Credited when PayHub confirms (webhook or return page)."""
    _require_kyc(user)
    if ngn_rail() != "payhub":
        raise FlowError("WRONG_RAIL", "Use your dedicated bank account to deposit naira.", 409)
    if not payhub.enabled():
        raise FlowError("PROVIDER_NOT_CONFIGURED", "Naira deposits are not available yet.", 503)
    credit = ledger.quantize(amount, "NGN", db)
    if credit < MIN_NGN_DEPOSIT:
        raise FlowError("AMOUNT_TOO_SMALL", f"The minimum deposit is NGN {MIN_NGN_DEPOSIT}.", 422)
    try:
        total = payhub.fee_quote(credit)["total"] if settings.PAYHUB_PASS_FEE_TO_PAYER else credit
        client_ref = "dep-" + secrets.token_hex(8)
        charge = payhub.create_charge(
            amount=total, customer_email=user.email, client_reference=client_ref,
            redirect_url=f"{settings.FRONTEND_URL.rstrip('/')}/dashboard/payouts?deposit=return",
            description=f"Bondrova wallet deposit · NGN {credit}",
            metadata={"user_id": str(user.id), "credit_amount": str(credit), "app": "bondrova"},
        )
    except (payhub.PayHubError, httpx.HTTPError) as exc:
        raise FlowError("DEPOSIT_START_FAILED", f"Couldn't start the payment: {exc}", 502)
    db.add(WalletTransaction(user_id=user.id, type="deposit", amount=float(credit), currency="NGN",
                             status="pending", method="payhub", reference=charge["reference"],
                             fee=float(total - credit) or None,  # processing fees the payer covered at checkout
                             description=f"Deposit via card/bank transfer (pays NGN {total} incl. fee)"))
    db.commit()
    return {"reference": charge["reference"], "checkout_url": charge["checkout_url"],
            "credit_amount": str(credit), "total": str(total), "fee": str(total - credit)}


def handle_payhub_charge(db: Session, reference: str) -> str:
    """Settle a PayHub charge we started. PayHub's API is the source of truth; credit exactly once."""
    txn = db.query(WalletTransaction).filter(WalletTransaction.reference == reference,
                                             WalletTransaction.type == "deposit",
                                             WalletTransaction.method == "payhub").with_for_update().first()
    if not txn:
        logger.error("PayHub charge %s has no matching Bondrova deposit", reference)
        return "unmatched"
    if txn.status == "completed":
        return "duplicate"
    c = payhub.get_charge(reference)
    if c["status"] == "success":
        if c["amount"] < ledger.to_decimal(txn.amount):
            logger.error("PayHub charge %s amount %s < expected credit %s", reference, c["amount"], txn.amount)
            txn.status, txn.flagged_high_risk = "failed", True
            db.commit()
            return "amount_mismatch"
        credited = ledger.credit(db, user_id=txn.user_id, currency="NGN", amount=txn.amount, to_bucket="available",
                                 reference_type="deposit:payhub", reference_id=reference,
                                 description=f"Deposit {reference}", idempotency_key=reference)
        if not credited:
            db.rollback()
            return "duplicate"
        txn.status = "completed"
        # This notification is also emailed (Bondrova-branded) — it is the user's receipt.
        added = float(ledger.quantize(txn.amount, "NGN", db))
        fee = float(txn.fee or 0)
        receipt = f"₦{added:,.2f} has been added to your wallet."
        if fee > 0:
            receipt += (f" You paid ₦{added + fee:,.2f} at checkout: ₦{added:,.2f} to your wallet plus "
                        f"₦{fee:,.2f} payment processing fees.")
        receipt += f" Reference: {reference}."
        create_notification(db, txn.user_id, "Deposit received — receipt", receipt, "payment")
        _receipt_copy(db, txn.user_id, "Deposit", [("Added to wallet", f"NGN {added:,.2f}"),
                                                   ("Paid at checkout", f"NGN {added + fee:,.2f}"),
                                                   ("Processing fees", f"NGN {fee:,.2f}"), ("Reference", reference)])
        db.commit()
        return "credited"
    if c["status"] == "failed":
        txn.status = "failed"
        db.commit()
        return "failed"
    return f"not_final:{c['status']}"


# ═════════════════════════════════════════════════════════════════════════════
#  DEPOSIT EVENTS (webhook → verify with provider → credit once)
# ═════════════════════════════════════════════════════════════════════════════

def _record_deposit(db: Session, *, user: User, currency: str, amount, method: str, reference: str,
                    description: str, flagged: bool = False) -> bool:
    credited = ledger.credit(
        db, user_id=user.id, currency=currency, amount=amount, to_bucket="available",
        reference_type=f"deposit:{method}", reference_id=reference, description=description,
        idempotency_key=reference,
    )
    if not credited:
        db.rollback()
        return False
    db.add(WalletTransaction(
        user_id=user.id, type="deposit", amount=float(ledger.quantize(amount, currency, db)),
        currency=currency, status="completed", description=description, method=method,
        reference=reference, flagged_high_risk=flagged,
    ))
    create_notification(db, user.id, "Deposit received",
                        f"{currency} {ledger.quantize(amount, currency, db)} has been added to your wallet.", "payment")
    db.commit()
    _receipt_copy(db, user.id, "Deposit", [("Added to wallet", f"{currency} {ledger.quantize(amount, currency, db)}"),
                                           ("Method", method), ("Reference", reference)])
    return True


def _receipt_copy(db: Session, user_id: str, kind: str, lines: list) -> None:
    from app.services import receipts
    u = db.get(User, user_id)
    if u:
        receipts.send_copy(db, kind=kind, user_name=f"{u.first_name or ''} {u.last_name or ''}".strip() or u.email,
                           user_email=u.email, lines=lines)


def handle_paystack_charge(db: Session, reference: str, hint: Optional[dict] = None) -> str:
    """charge.success (checkout or dedicated account). Returns a short outcome string."""
    tx = paystack_rails.verify_transaction(reference)
    if tx["status"] != "success":
        return f"not_successful:{tx['status']}"

    user = None
    user_id = (tx.get("metadata") or {}).get("user_id")
    if user_id:
        user = db.query(User).filter(User.id == str(user_id)).first()
    if not user and tx.get("customer_code"):
        va = db.query(VirtualAccount).filter_by(provider="paystack", provider_customer_code=tx["customer_code"]).first()
        if va:
            user = db.query(User).filter(User.id == va.user_id).first()
    if not user and tx.get("customer_email"):
        user = db.query(User).filter(User.email == tx["customer_email"].lower()).first()
    if not user:
        logger.error("Paystack charge %s: no matching user — needs manual review", reference)
        return "unmatched"

    # Bank transfers into a dedicated account from someone else's name get flagged for review.
    flagged = False
    sender = ((hint or {}).get("authorization") or {}).get("sender_name") or ""
    if tx.get("channel") == "dedicated_nuban" and sender and not names_match(user.first_name, user.last_name, sender):
        flagged = True

    ok = _record_deposit(db, user=user, currency=tx["currency"], amount=tx["amount"], method="paystack",
                         reference=reference, flagged=flagged,
                         description=f"Bank deposit{' from ' + sender if sender else ''} ({reference})")
    return "credited" if ok else "duplicate"


def handle_quidax_deposit(db: Session, quidax_user_id: str, deposit_id: str) -> str:
    pc = db.query(ProviderCustomer).filter_by(provider="quidax", provider_customer_id=str(quidax_user_id)).first()
    if not pc:
        logger.error("Quidax deposit %s for unknown sub-account %s", deposit_id, quidax_user_id)
        return "unmatched"
    d = quidax.fetch_deposit(quidax_user_id=quidax_user_id, deposit_id=deposit_id)
    if d["status"] not in quidax.DEPOSIT_DONE_STATES:
        return f"not_final:{d['status']}"
    if d["amount"] <= 0:
        return "zero_amount"
    user = db.query(User).filter(User.id == pc.user_id).first()
    ok = _record_deposit(db, user=user, currency=d["currency"], amount=d["amount"], method="quidax",
                         reference=f"quidax:{d['id']}",
                         description=f"{d['currency']} deposit ({(d['network'] or '').upper()}) tx {str(d['txid'] or '')[:18]}")
    if ok:
        # Move the coins from the user's sub-account into the master account (pooled custody).
        try:
            if ledger.claim_event(db, "quidax_sweep", d["id"]):
                quidax.sweep_to_master(quidax_user_id=quidax_user_id, currency=d["currency"],
                                       amount=d["amount"], reference=f"sweep-{d['id']}")
                db.commit()
        except Exception as exc:  # the credit stands; the sweep is retried by reconciliation
            db.rollback()
            logger.warning("Quidax sweep for deposit %s failed: %s", d["id"], exc)
    return "credited" if ok else "duplicate"


# ═════════════════════════════════════════════════════════════════════════════
#  PAYOUT DESTINATIONS (address book)
# ═════════════════════════════════════════════════════════════════════════════

def _new_confirmation(dest: PayoutDestination) -> str:
    raw = secrets.token_urlsafe(32)
    dest.confirm_token_hash = _hash_token(raw)
    dest.confirm_expires_at = datetime.utcnow() + timedelta(hours=1)
    return raw


def add_bank_destination(db: Session, user: User, *, bank_code: str, account_number: str,
                         label: Optional[str]) -> tuple[PayoutDestination, str]:
    _require_kyc(user)
    account_number = re.sub(r"\D", "", account_number or "")
    if len(account_number) != 10:
        raise FlowError("INVALID_ACCOUNT", "Enter a valid 10-digit account number.", 422)
    if not ngn_enabled():
        raise FlowError("PROVIDER_NOT_CONFIGURED", "Bank payouts are not available yet.", 503)
    banks = {b["code"]: b["name"] for b in ngn_banks()}
    payhub_account_id = None
    try:
        if ngn_rail() == "payhub":
            acct = payhub.register_bank_account(bank_code=bank_code, bank_name=banks.get(bank_code) or bank_code,
                                                account_number=account_number, external_ref=str(user.id))
            if acct["status"] != "verified" or not acct["account_name"]:
                raise FlowError("ACCOUNT_LOOKUP_FAILED",
                                f"We couldn't verify that account: {acct.get('error') or 'not found'}", 422)
            resolved, payhub_account_id = acct["account_name"], acct["id"]
        else:
            resolved = paystack_rails.resolve_account(account_number=account_number, bank_code=bank_code)
    except (paystack_rails.PaystackError, payhub.PayHubError, httpx.HTTPError) as exc:
        raise FlowError("ACCOUNT_LOOKUP_FAILED", f"We couldn't verify that account: {exc}", 422)
    if not names_match(user.first_name, user.last_name, resolved):
        raise FlowError("NAME_MISMATCH",
                        f"This account belongs to '{resolved}', which doesn't match your verified name. "
                        "You can only withdraw to an account in your own name.", 422)
    dest = PayoutDestination(user_id=user.id, kind="bank", currency="NGN", label=label,
                             bank_code=bank_code, bank_name=banks.get(bank_code), account_number=account_number,
                             account_name=resolved, status="pending_confirmation",
                             provider_recipient_code=payhub_account_id)
    db.add(dest)
    db.flush()
    return dest, _new_confirmation(dest)


def _safe_banks() -> list[dict]:
    try:
        return paystack_rails.list_banks()
    except Exception:
        return []


def add_crypto_destination(db: Session, user: User, *, currency: str, network: str, address: str,
                           destination_tag: Optional[str], label: Optional[str]) -> tuple[PayoutDestination, str]:
    _require_kyc(user)
    currency, network, address = currency.upper(), network.lower(), (address or "").strip()
    if (currency, network) not in quidax.rails():
        raise FlowError("UNSUPPORTED_RAIL", f"{currency} on {network.upper()} isn't supported.", 422)
    if not valid_address(network, address):
        raise FlowError("INVALID_ADDRESS", f"That isn't a valid {network.upper()} address.", 422)
    if db.query(UserDepositAddress).filter(UserDepositAddress.address == address).first():
        raise FlowError("INTERNAL_ADDRESS", "That's a Bondrova deposit address — use your own external wallet.", 422)
    dest = PayoutDestination(user_id=user.id, kind="crypto", currency=currency, network=network,
                             address=address, destination_tag=destination_tag, label=label,
                             status="pending_confirmation")
    db.add(dest)
    db.flush()
    return dest, _new_confirmation(dest)


def confirm_destination(db: Session, raw_token: str) -> PayoutDestination:
    dest = db.query(PayoutDestination).filter_by(confirm_token_hash=_hash_token(raw_token)).first()
    if not dest or dest.status != "pending_confirmation":
        raise FlowError("INVALID_TOKEN", "This confirmation link is invalid or was already used.", 400)
    if dest.confirm_expires_at and dest.confirm_expires_at < datetime.utcnow():
        raise FlowError("TOKEN_EXPIRED", "This confirmation link has expired. Add the destination again.", 400)
    now = datetime.utcnow()
    dest.status = "confirmed"
    dest.confirmed_at = now
    dest.usable_from = now + timedelta(hours=settings.PAYOUT_DESTINATION_COOLDOWN_HOURS)
    dest.confirm_token_hash = None
    # Security lock: a new destination pauses ALL withdrawals for the cooldown (Bybit-style).
    user = db.query(User).filter(User.id == dest.user_id).first()
    if user and (not user.withdrawal_cooldown_until or user.withdrawal_cooldown_until < dest.usable_from):
        user.withdrawal_cooldown_until = dest.usable_from
    create_notification(db, dest.user_id, "New withdrawal destination added",
                        f"A new withdrawal destination was confirmed. For your security, withdrawals are paused until "
                        f"{dest.usable_from:%Y-%m-%d %H:%M} UTC. If this wasn't you, contact support immediately.",
                        "security")
    db.commit()
    return dest


# ═════════════════════════════════════════════════════════════════════════════
#  WITHDRAWALS & PAYOUTS
# ═════════════════════════════════════════════════════════════════════════════

def create_withdrawal(db: Session, user: User, *, destination_id: str, amount) -> WalletTransaction:
    """Caller has already checked frozen/blocked/cooldown/2FA. Moves available → pending."""
    _require_kyc(user)
    dest = db.query(PayoutDestination).filter_by(id=destination_id, user_id=user.id).first()
    if not dest or dest.status == "removed":
        raise FlowError("DESTINATION_NOT_FOUND", "Withdrawal destination not found.", 404)
    if dest.status != "confirmed":
        raise FlowError("DESTINATION_UNCONFIRMED", "Confirm this destination from the email we sent first.", 409)
    if not dest.is_usable:
        raise FlowError("DESTINATION_COOLDOWN",
                        f"For your security this destination can be used from {dest.usable_from:%Y-%m-%d %H:%M} UTC.", 429)
    amt = ledger.quantize(amount, dest.currency, db)
    if amt <= 0:
        raise FlowError("INVALID_AMOUNT", "Amount must be greater than zero.", 422)

    target = (f"{dest.bank_name or 'Bank'} ••••{(dest.account_number or '')[-4:]}" if dest.kind == "bank"
              else f"{dest.network.upper()} {dest.address[:6]}…{dest.address[-4:]}")
    from app.services import fees
    fee = fees.withdrawal_fee(db, amt, dest.currency)
    if fee >= amt:
        raise FlowError("AMOUNT_BELOW_FEE", f"The withdrawal fee is {dest.currency} {fee}; withdraw more than that.", 422)
    txn = WalletTransaction(user_id=user.id, type="withdrawal", amount=float(amt), currency=dest.currency,
                            status="pending", method=dest.kind, payout_destination_id=dest.id, fee=float(fee) or None,
                            description=f"Withdrawal to {target}" + (f" (fee {dest.currency} {fee})" if fee > 0 else ""))
    db.add(txn)
    db.flush()
    try:
        ledger.move(db, user_id=user.id, currency=dest.currency, amount=amt, from_bucket="available",
                    to_bucket="pending", reference_type="withdrawal_request", reference_id=str(txn.id),
                    description=txn.description, idempotency_key=str(txn.id))
    except ledger.InsufficientFunds as e:
        db.rollback()
        raise FlowError("INSUFFICIENT_BALANCE",
                        f"Your {dest.currency} balance ({e.have}) is less than {amt}.", 422)
    db.commit()
    # Every withdrawal waits for a finance admin's approval (Admin → Withdrawals) before any
    # money is sent — there is deliberately no automatic payout path.
    return txn


def _provider_for(dest: PayoutDestination) -> str:
    return ngn_rail() if dest.kind == "bank" else "quidax"


def dispatch_payout(db: Session, txn: WalletTransaction) -> str:
    """Send an approved withdrawal to the provider. Completion comes later via webhook/poll."""
    txn = db.query(WalletTransaction).filter(WalletTransaction.id == txn.id).with_for_update().one()
    if txn.status != "pending":
        return f"skipped:{txn.status}"
    dest = db.query(PayoutDestination).filter_by(id=txn.payout_destination_id).first()
    # The user receives the amount minus the withdrawal fee (kept by the platform on settlement).
    payout_amount = round(float(txn.amount) - float(txn.fee or 0), 8)
    if not dest:
        raise FlowError("DESTINATION_NOT_FOUND", "Withdrawal destination not found.", 404)
    reference = str(txn.id)
    try:
        if dest.kind == "bank" and ngn_rail() == "payhub":
            if not dest.provider_recipient_code:
                acct = payhub.register_bank_account(bank_code=dest.bank_code, bank_name=dest.bank_name or dest.bank_code,
                                                    account_number=dest.account_number, external_ref=str(txn.user_id))
                dest.provider_recipient_code = acct["id"]
            res = payhub.request_payout(bank_account_id=dest.provider_recipient_code, amount=payout_amount,
                                        client_reference=reference)
            txn.provider_payout_id = res["id"]
            if res["status"] in payhub.PAYOUT_FAILED:
                db.commit()
                return settle_payout(db, reference, success=False,
                                     reason=res.get("failure_reason") or "Payout was refused by the gateway")
        elif dest.kind == "bank":
            if not dest.provider_recipient_code:
                dest.provider_recipient_code = paystack_rails.create_recipient(
                    name=dest.account_name, account_number=dest.account_number, bank_code=dest.bank_code)
            res = paystack_rails.initiate_transfer(amount=payout_amount, recipient_code=dest.provider_recipient_code,
                                                   reference=reference, reason="Bondrova withdrawal")
            txn.provider_payout_id = res.get("transfer_code")
        else:
            res = quidax.create_withdrawal(from_user="me", currency=dest.currency, amount=payout_amount,
                                           address=dest.address, network=dest.network, reference=reference,
                                           note="Bondrova withdrawal", destination_tag=dest.destination_tag)
            txn.provider_payout_id = res.get("id")
        txn.status = "processing"
        txn.failure_reason = None
        db.commit()
        return "processing"
    except httpx.TimeoutException:
        # Outcome unknown — never retry blindly. Mark processing; the poller asks the provider by reference.
        txn.status = "processing"
        txn.failure_reason = "Provider timeout — awaiting status check"
        db.commit()
        return "processing_unknown"
    except (paystack_rails.PaystackError, payhub.PayHubError, quidax.QuidaxError, httpx.HTTPError) as exc:
        # Provider explicitly refused: stays pending for an admin to retry or reject.
        db.rollback()
        txn = db.query(WalletTransaction).filter(WalletTransaction.id == reference).one()
        txn.failure_reason = f"{_provider_for(dest)} rejected payout: {exc}"[:500]
        db.commit()
        logger.error("Payout %s rejected by provider: %s", reference, exc)
        return "provider_rejected"


def settle_payout(db: Session, withdrawal_id: str, *, success: bool, reason: str = "") -> str:
    """Final state from the provider. Exactly once per withdrawal."""
    txn = db.query(WalletTransaction).filter(WalletTransaction.id == withdrawal_id,
                                             WalletTransaction.type == "withdrawal").with_for_update().first()
    if not txn:
        return "unknown_withdrawal"
    if txn.status in ("completed", "failed"):
        return f"already_{txn.status}"
    if success:
        from app.services import fees
        fees.debit_withdrawal(db, txn)
        txn.status = "completed"
        net = round(float(txn.amount) - float(txn.fee or 0), 2)
        _receipt_copy(db, txn.user_id, "Withdrawal", [("Sent", f"{txn.currency} {net:,.2f}"),
                                                      ("Fee", f"{txn.currency} {float(txn.fee or 0):,.2f}"),
                                                      ("To", txn.description or ""), ("Reference", str(txn.id))])
        create_notification(db, txn.user_id, "Withdrawal sent",
                            f"Your withdrawal of {txn.currency} {net:,.2f} has been sent"
                            + (f" (fee {txn.currency} {float(txn.fee):,.2f})." if txn.fee else "."), "payment")
    else:
        ledger.move(db, user_id=txn.user_id, currency=txn.currency, amount=txn.amount, from_bucket="pending",
                    to_bucket="available", reference_type="withdrawal_reversal", reference_id=str(txn.id),
                    description=f"Payout failed: {reason[:100]}", idempotency_key=str(txn.id))
        txn.status = "failed"
        txn.failure_reason = reason[:500] or "Payout failed"
        create_notification(db, txn.user_id, "Withdrawal failed",
                            f"Your withdrawal of {txn.currency} {txn.amount} couldn't be completed and the funds "
                            f"are back in your wallet. {reason[:120]}", "payment")
    db.commit()
    return txn.status


def handle_paystack_transfer(db: Session, reference: str) -> str:
    t = paystack_rails.verify_transfer(reference)
    if t["status"] == "success":
        return settle_payout(db, reference, success=True)
    if t["status"] in ("failed", "reversed"):
        return settle_payout(db, reference, success=False, reason=f"Bank transfer {t['status']}")
    return f"not_final:{t['status']}"


def handle_payhub_payout(db: Session, payout_id: str) -> str:
    p = payhub.get_payout(payout_id)
    withdrawal_id = p.get("client_reference")
    if not withdrawal_id:
        txn = db.query(WalletTransaction).filter(WalletTransaction.provider_payout_id == payout_id).first()
        withdrawal_id = str(txn.id) if txn else None
    if not withdrawal_id:
        return "unmatched"
    if p["status"] in payhub.PAYOUT_DONE:
        return settle_payout(db, withdrawal_id, success=True)
    if p["status"] in payhub.PAYOUT_FAILED:
        return settle_payout(db, withdrawal_id, success=False, reason=p.get("failure_reason") or "Bank payout failed")
    return f"not_final:{p['status']}"


def handle_quidax_withdrawal(db: Session, reference: str) -> str:
    if reference.startswith("sweep-"):
        return "sweep"
    w = quidax.fetch_withdrawal_by_reference(from_user="me", reference=reference)
    if w["status"] in quidax.WITHDRAWAL_DONE_STATES:
        return settle_payout(db, reference, success=True)
    if w["status"] in quidax.WITHDRAWAL_FAILED_STATES:
        return settle_payout(db, reference, success=False, reason=w.get("reason") or "Crypto withdrawal rejected")
    return f"not_final:{w['status']}"


def poll_processing_payouts(db: Session) -> dict:
    """Backstop for missed webhooks: ask providers about every in-flight payout."""
    out = {"checked": 0, "settled": 0}
    rows = db.query(WalletTransaction).filter(WalletTransaction.type == "withdrawal",
                                              WalletTransaction.status == "processing").all()
    for txn in rows:
        out["checked"] += 1
        dest = db.query(PayoutDestination).filter_by(id=txn.payout_destination_id).first()
        if not dest:
            continue
        try:
            if dest.kind == "bank" and ngn_rail() == "payhub":
                res = handle_payhub_payout(db, txn.provider_payout_id) if txn.provider_payout_id else "no_payout_id"
            elif dest.kind == "bank":
                res = handle_paystack_transfer(db, str(txn.id))
            else:
                res = handle_quidax_withdrawal(db, str(txn.id))
            if res in ("completed", "failed"):
                out["settled"] += 1
        except Exception as exc:
            db.rollback()
            logger.warning("Payout poll %s: %s", txn.id, exc)
    return out
