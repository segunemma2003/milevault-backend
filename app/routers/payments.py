"""
User-facing payment rails + provider webhooks.

  GET  /payments/rails                      what's available + the user's deposit details
  POST /payments/virtual-account            create my NGN bank account (Paystack DVA)
  POST /payments/deposit-address            create my crypto deposit address (Quidax)
  GET  /payments/banks                      Nigerian banks (for adding a payout account)
  GET  /payments/destinations               my saved withdrawal destinations
  POST /payments/destinations/bank          add a bank account (name must match KYC)
  POST /payments/destinations/crypto        add a crypto address
  POST /payments/destinations/confirm       confirm from the emailed link
  DELETE /payments/destinations/{id}        remove
  POST /payments/withdraw                   withdraw to a saved destination
  POST /payments/webhooks/quidax            Quidax events (deposits, withdrawals)
"""
from __future__ import annotations

import html
import json
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.payments import PayoutDestination, UserDepositAddress, VirtualAccount
from app.models.user import User
from app.services import payhub, paystack_rails, payment_flows, quidax
from app.services.payment_flows import FlowError

router = APIRouter(prefix="/payments", tags=["Payments"])
log = logging.getLogger("bondrova.payments")


def _raise(e: FlowError):
    raise HTTPException(status_code=e.status, detail={"error": e.code, "message": e.message})


def _va_out(va: Optional[VirtualAccount]):
    if not va:
        return None
    return {"status": va.status, "account_number": va.account_number, "account_name": va.account_name,
            "bank_name": va.bank_name, "currency": va.currency, "failure_reason": va.failure_reason}


def _addr_out(a: UserDepositAddress):
    return {"currency": a.currency, "network": a.network, "address": a.address,
            "destination_tag": a.destination_tag, "status": a.status, "failure_reason": a.failure_reason}


def _dest_out(d: PayoutDestination):
    return {
        "id": d.id, "kind": d.kind, "currency": d.currency, "label": d.label,
        "bank_name": d.bank_name, "account_number_last4": (d.account_number or "")[-4:] or None,
        "account_name": d.account_name, "network": d.network, "address": d.address,
        "destination_tag": d.destination_tag, "status": d.status, "usable": d.is_usable,
        "usable_from": d.usable_from, "created_at": d.created_at,
    }


# ─── Deposit rails ────────────────────────────────────────────────────────────

@router.get("/rails")
def rails(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    va = db.query(VirtualAccount).filter_by(user_id=current_user.id, provider="paystack", currency="NGN").first()
    addrs = db.query(UserDepositAddress).filter_by(user_id=current_user.id).all()
    return {
        "kyc_verified": bool(current_user.is_kyc_verified),
        "ngn_rail": payment_flows.ngn_rail(),
        "bank_deposits_enabled": payment_flows.ngn_enabled(),
        "crypto_enabled": quidax.enabled(),
        "crypto_rails": [{"currency": c, "network": n} for c, n in quidax.rails()],
        "virtual_account": _va_out(va),
        "deposit_addresses": [_addr_out(a) for a in addrs],
        "destination_cooldown_hours": settings.PAYOUT_DESTINATION_COOLDOWN_HOURS,
    }


@router.post("/virtual-account")
def create_virtual_account(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return _va_out(payment_flows.ensure_virtual_account(db, current_user))
    except FlowError as e:
        _raise(e)


class AddressRequest(BaseModel):
    currency: str
    network: str


@router.post("/deposit-address")
def create_deposit_address(body: AddressRequest, current_user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    try:
        return _addr_out(payment_flows.ensure_deposit_address(db, current_user, body.currency, body.network))
    except FlowError as e:
        _raise(e)


@router.get("/banks")
def banks(current_user: User = Depends(get_current_user)):
    if not payment_flows.ngn_enabled():
        return []
    rows = payment_flows.ngn_banks()
    if not rows:
        raise HTTPException(status_code=502, detail={"error": "BANKS_UNAVAILABLE", "message": "Bank list is unavailable right now."})
    return rows


class NairaDepositRequest(BaseModel):
    amount: float


@router.get("/deposit/naira/quote")
def naira_deposit_quote(amount: float, current_user: User = Depends(get_current_user)):
    """What the payer will pay at checkout for `amount` to land in the wallet (fees shown up front)."""
    from app.services import payhub
    if amount <= 0:
        raise HTTPException(status_code=422, detail={"error": "INVALID_AMOUNT", "message": "Enter an amount."})
    if not payhub.enabled() or not settings.PAYHUB_PASS_FEE_TO_PAYER:
        return {"amount": f"{amount:.2f}", "total": f"{amount:.2f}", "fee": "0.00"}
    try:
        q = payhub.fee_quote(amount)
    except Exception:
        raise HTTPException(status_code=502, detail={"error": "QUOTE_FAILED", "message": "Couldn't get the fee right now."})
    return {"amount": str(q["amount"]), "total": str(q["total"]), "fee": str(q["fee"])}


@router.post("/deposit/naira")
def start_naira_deposit(body: NairaDepositRequest, current_user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Returns a checkout URL (card or bank transfer). The wallet is credited when PayHub confirms."""
    if current_user.wallet_frozen:
        raise HTTPException(status_code=403, detail={"error": "WALLET_FROZEN", "message": "Your wallet has been frozen."})
    try:
        return payment_flows.start_naira_deposit(db, current_user, body.amount)
    except FlowError as e:
        _raise(e)


class ConfirmDepositRequest(BaseModel):
    reference: str


@router.post("/deposit/confirm")
def confirm_naira_deposit(body: ConfirmDepositRequest, current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    """Called by the return page after checkout. Safe to repeat; the webhook usually gets there first."""
    from app.models.wallet import WalletTransaction
    txn = db.query(WalletTransaction).filter(WalletTransaction.reference == body.reference,
                                             WalletTransaction.user_id == current_user.id,
                                             WalletTransaction.type == "deposit").first()
    if not txn:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Deposit not found."})
    if txn.status in ("completed", "failed"):
        return {"status": txn.status, "amount": txn.amount}
    try:
        outcome = payment_flows.handle_payhub_charge(db, body.reference)
    except Exception as exc:
        db.rollback()
        log.warning("deposit confirm %s: %s", body.reference, exc)
        outcome = "pending"
    db.refresh(txn)
    return {"status": txn.status, "amount": txn.amount, "outcome": outcome}


# ─── Destinations (address book) ──────────────────────────────────────────────

class BankDestinationRequest(BaseModel):
    bank_code: str
    account_number: str
    label: Optional[str] = Field(None, max_length=100)
    otp_code: Optional[str] = None


class CryptoDestinationRequest(BaseModel):
    currency: str
    network: str
    address: str
    destination_tag: Optional[str] = None
    label: Optional[str] = Field(None, max_length=100)
    otp_code: Optional[str] = None


def _send_confirmation(to_email: str, first_name: str, raw_token: str, summary: str) -> None:
    from app.routers.auth import _send_email_direct
    from app.services.email_template import button
    url = f"{settings.FRONTEND_URL}/dashboard/payouts/confirm?token={raw_token}"
    body = (
        f"<h2>Confirm your new withdrawal destination</h2>"
        f"<p>Hi {html.escape(first_name or 'there')},</p>"
        f"<p>You asked to add this withdrawal destination to your Bondrova account:</p>"
        f"<p><strong>{html.escape(summary)}</strong></p>"
        f"<p>{button(url, 'Confirm destination')}</p>"
        f"<p>This link expires in 1 hour. For your security, withdrawals are paused for "
        f"{settings.PAYOUT_DESTINATION_COOLDOWN_HOURS} hours after you confirm.</p>"
        f"<p><strong>Didn't request this?</strong> Don't click the link — change your password and contact support.</p>"
    )
    _send_email_direct(to_email, "Confirm your new withdrawal destination", body)


def _gate(db: Session, user: User, otp_code: Optional[str]) -> None:
    """Adding a destination requires the same checks as withdrawing (incl. 2FA when enabled), minus cooldown."""
    from app.routers.wallet import check_withdrawal_allowed
    check_withdrawal_allowed(db, user, otp_code, ignore_cooldown=True)


@router.get("/destinations")
def list_destinations(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (db.query(PayoutDestination)
            .filter(PayoutDestination.user_id == current_user.id, PayoutDestination.status != "removed")
            .order_by(PayoutDestination.created_at.desc()).all())
    return [_dest_out(d) for d in rows]


@router.post("/destinations/bank", status_code=201)
def add_bank(body: BankDestinationRequest, background: BackgroundTasks,
             current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _gate(db, current_user, body.otp_code)
    try:
        dest, token = payment_flows.add_bank_destination(db, current_user, bank_code=body.bank_code,
                                                         account_number=body.account_number, label=body.label)
    except FlowError as e:
        db.rollback()
        _raise(e)
    db.commit()
    background.add_task(_send_confirmation, current_user.email, current_user.first_name, token,
                        f"{dest.bank_name or 'Bank'} — {dest.account_name} ({dest.account_number})")
    return _dest_out(dest)


@router.post("/destinations/crypto", status_code=201)
def add_crypto(body: CryptoDestinationRequest, background: BackgroundTasks,
               current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _gate(db, current_user, body.otp_code)
    try:
        dest, token = payment_flows.add_crypto_destination(
            db, current_user, currency=body.currency, network=body.network, address=body.address,
            destination_tag=body.destination_tag, label=body.label)
    except FlowError as e:
        db.rollback()
        _raise(e)
    db.commit()
    background.add_task(_send_confirmation, current_user.email, current_user.first_name, token,
                        f"{dest.currency} on {dest.network.upper()} — {dest.address}")
    return _dest_out(dest)


class ConfirmRequest(BaseModel):
    token: str


@router.post("/destinations/confirm")
def confirm(body: ConfirmRequest, db: Session = Depends(get_db)):
    try:
        return _dest_out(payment_flows.confirm_destination(db, body.token.strip()))
    except FlowError as e:
        _raise(e)


@router.delete("/destinations/{destination_id}")
def remove(destination_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    d = db.query(PayoutDestination).filter_by(id=destination_id, user_id=current_user.id).first()
    if not d:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Destination not found."})
    d.status = "removed"
    d.confirm_token_hash = None
    db.commit()
    return {"ok": True}


# ─── Withdraw ─────────────────────────────────────────────────────────────────

class WithdrawToDestinationRequest(BaseModel):
    destination_id: str
    amount: float
    otp_code: Optional[str] = None


@router.post("/withdraw")
def withdraw(body: WithdrawToDestinationRequest, current_user: User = Depends(get_current_user),
             db: Session = Depends(get_db)):
    from app.routers.wallet import check_withdrawal_allowed
    check_withdrawal_allowed(db, current_user, body.otp_code)
    try:
        txn = payment_flows.create_withdrawal(db, current_user, destination_id=body.destination_id, amount=body.amount)
    except FlowError as e:
        _raise(e)
    db.refresh(txn)
    return {
        "id": txn.id, "status": txn.status, "amount": txn.amount, "currency": txn.currency,
        "message": ("Your withdrawal is on its way." if txn.status == "processing"
                    else "Withdrawal requested. It will be reviewed and sent shortly."),
    }


# ─── PayHub webhook (all naira events for Bondrova, forwarded by the central gateway) ──

@router.post("/webhooks/payhub")
async def payhub_webhook(request: Request, db: Session = Depends(get_db),
                         x_payhub_signature: Optional[str] = Header(default=None),
                         x_payhub_event: Optional[str] = Header(default=None)):
    raw = await request.body()
    if not payhub.verify_webhook(raw, x_payhub_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    event = (payload.get("type") or x_payhub_event or "").lower()
    data = payload.get("data") or {}
    try:
        if event.startswith("charge.") and data.get("reference"):
            outcome = payment_flows.handle_payhub_charge(db, data["reference"])
        elif event in ("payout.completed", "payout.failed", "payout.rejected") and data.get("id"):
            outcome = payment_flows.handle_payhub_payout(db, data["id"])
        elif event in ("identity.verified", "identity.failed"):
            from app.routers.kyc import handle_identity_event
            outcome = handle_identity_event(db, event, data)
        else:
            outcome = "ignored"
    except Exception as exc:
        db.rollback()
        log.error("PayHub webhook %s failed: %s", event, exc)
        raise HTTPException(status_code=500, detail="Webhook processing failed")  # PayHub retries
    log.warning("PayHub webhook %s %s → %s", event, payload.get("id"), outcome)
    return {"ok": True, "outcome": outcome}


# ─── Quidax webhook ───────────────────────────────────────────────────────────

@router.post("/webhooks/quidax")
async def quidax_webhook(request: Request, db: Session = Depends(get_db),
                         quidax_signature: Optional[str] = Header(default=None)):
    raw = await request.body()
    if not quidax.verify_webhook(raw, quidax_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    event = (payload.get("event") or "").lower()
    data = payload.get("data") or {}
    try:
        if event.startswith("deposit.") and event.endswith(("successful", "success", "completed")):
            user_id = str((data.get("user") or {}).get("id") or data.get("user_id") or "")
            outcome = payment_flows.handle_quidax_deposit(db, user_id, str(data.get("id")))
        elif event.startswith("withdraw"):
            ref = data.get("reference") or ""
            outcome = payment_flows.handle_quidax_withdrawal(db, ref) if ref else "no_reference"
        else:
            outcome = "ignored"
    except Exception as exc:
        db.rollback()
        log.error("Quidax webhook %s failed: %s", event, exc)
        raise HTTPException(status_code=500, detail="Webhook processing failed")  # Quidax retries
    log.warning("Quidax webhook %s → %s", event, outcome)
    return {"ok": True, "outcome": outcome}
