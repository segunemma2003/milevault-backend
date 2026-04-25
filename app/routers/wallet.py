"""
Wallet router — balances, deposit (via payment gateway), withdrawal, conversion.
Exchange rates are admin-controlled via the ExchangeRate DB table.
Deposit flow:
  1. POST /wallet/deposit/initiate  →  get gateway redirect URL / client_secret
  2. User completes payment on gateway
  3. POST /wallet/deposit/verify    →  verify + credit wallet
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request, Header
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timedelta
from app.database import get_db
from app.schemas.wallet import DepositRequest, WithdrawRequest, ConvertRequest, TransferRequest
from app.models.wallet import WalletBalance, WalletTransaction
from app.models.currency import Currency, ExchangeRate
from app.models.user import User
from app.dependencies import get_current_user
from app.services.notification_service import create_notification
from app.services.payment_service import (
    initiate_deposit,
    verify_deposit,
    get_gateway_for_country,
    paystack_verify_webhook,
    PaystackError,
    StripeError,
    FlutterwaveError,
)
from app.config import settings

router = APIRouter(prefix="/wallet", tags=["Wallet"])

CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$",
    "NGN": "₦", "GHS": "GH₵", "KES": "KSh", "ZAR": "R",
    "BTC": "₿", "ETH": "Ξ", "USDT": "₮", "USDC": "$",
}


def get_or_create_balance(db: Session, user_id: str, currency: str) -> WalletBalance:
    balance = db.query(WalletBalance).filter(
        WalletBalance.user_id == user_id, WalletBalance.currency == currency
    ).first()
    if not balance:
        balance = WalletBalance(user_id=user_id, currency=currency, amount=0.0)
        db.add(balance)
        db.flush()
    return balance


def _apply_verified_deposit(
    *,
    db: Session,
    user_id: str,
    gateway: str,
    reference: str,
    amount: float,
    currency: str,
    wallet_transaction_id: Optional[str] = None,
) -> tuple[WalletTransaction, WalletBalance]:
    """
    Idempotently credit wallet for a verified gateway payment.
    - Reuses pending tx by wallet_transaction_id, then by reference.
    - Prevents duplicate credit if tx is already completed.
    """
    txn = None
    if wallet_transaction_id:
        txn = db.query(WalletTransaction).filter(
            WalletTransaction.id == wallet_transaction_id,
            WalletTransaction.user_id == user_id,
            WalletTransaction.type == "deposit",
        ).first()

    if not txn:
        txn = db.query(WalletTransaction).filter(
            WalletTransaction.user_id == user_id,
            WalletTransaction.type == "deposit",
            WalletTransaction.reference == reference,
        ).order_by(WalletTransaction.created_at.desc()).first()

    if not txn:
        # Legacy recovery: older rows were created with empty reference.
        txn = db.query(WalletTransaction).filter(
            WalletTransaction.user_id == user_id,
            WalletTransaction.type == "deposit",
            WalletTransaction.status == "pending",
            WalletTransaction.method == gateway,
            WalletTransaction.reference.is_(None) | (WalletTransaction.reference == ""),
        ).order_by(WalletTransaction.created_at.desc()).first()

    if txn and txn.status == "completed":
        balance = get_or_create_balance(db, user_id, currency.upper())
        return txn, balance

    balance = get_or_create_balance(db, user_id, currency.upper())
    balance.amount += amount

    user = db.query(User).filter(User.id == user_id).first()
    if user:
        cooldown = datetime.utcnow() + timedelta(hours=24)
        if not user.withdrawal_cooldown_until or user.withdrawal_cooldown_until < cooldown:
            user.withdrawal_cooldown_until = cooldown

    if txn:
        txn.status = "completed"
        txn.amount = amount
        txn.currency = currency.upper()
        txn.method = gateway
        txn.reference = reference
        if not txn.description:
            txn.description = f"{gateway.title()} deposit verified"
    else:
        txn = WalletTransaction(
            user_id=user_id,
            type="deposit",
            amount=amount,
            currency=currency.upper(),
            status="completed",
            description=f"{gateway.title()} deposit verified",
            method=gateway,
            reference=reference,
        )
        db.add(txn)

    db.commit()
    db.refresh(balance)
    db.refresh(txn)
    return txn, balance


def _get_exchange_rate(db: Session, from_code: str, to_code: str) -> float:
    """
    Get rate from admin-controlled ExchangeRate table.
    Falls back to 1.0 (same currency) or raises if pair not found.
    """
    if from_code == to_code:
        return 1.0

    from_cur = db.query(Currency).filter(Currency.code == from_code, Currency.is_active == True).first()
    to_cur = db.query(Currency).filter(Currency.code == to_code, Currency.is_active == True).first()

    if not from_cur or not to_cur:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "CURRENCY_NOT_FOUND",
                "message": f"Currency '{from_code}' or '{to_code}' is not active on this platform.",
            },
        )

    rate_row = db.query(ExchangeRate).filter(
        ExchangeRate.from_currency_id == from_cur.id,
        ExchangeRate.to_currency_id == to_cur.id,
        ExchangeRate.is_active == True,
    ).first()

    if not rate_row:
        # Try inverse rate
        inverse = db.query(ExchangeRate).filter(
            ExchangeRate.from_currency_id == to_cur.id,
            ExchangeRate.to_currency_id == from_cur.id,
            ExchangeRate.is_active == True,
        ).first()
        if inverse:
            spread = 1 + (inverse.spread_percent / 100)
            return round((1 / inverse.rate) * spread, 8)

        raise HTTPException(
            status_code=422,
            detail={
                "error": "NO_EXCHANGE_RATE",
                "message": f"No exchange rate configured for {from_code} → {to_code}. Contact support.",
            },
        )

    spread = 1 + (rate_row.spread_percent / 100)
    return round(rate_row.rate * spread, 8)


# ─── Balance & history ────────────────────────────────────────────────────────

@router.get("/balances")
def get_balances(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    balances = db.query(WalletBalance).filter(WalletBalance.user_id == current_user.id).all()
    return [
        {
            "id": str(b.id),
            "currency": b.currency,
            "amount": b.amount,
            "escrow_amount": b.escrow_amount or 0,
            "pending_amount": b.pending_amount,
            "symbol": CURRENCY_SYMBOLS.get(b.currency, b.currency),
        }
        for b in balances
    ]


@router.get("/transactions")
def get_wallet_transactions(
    type: Optional[str] = Query(None, description="Filter by type: deposit, withdrawal, conversion"),
    currency: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(WalletTransaction).filter(WalletTransaction.user_id == current_user.id)
    if type:
        query = query.filter(WalletTransaction.type == type)
    if currency:
        query = query.filter(WalletTransaction.currency == currency.upper())
    txns = query.order_by(WalletTransaction.created_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "id": str(t.id),
            "type": t.type,
            "amount": t.amount,
            "currency": t.currency,
            "status": t.status,
            "description": t.description,
            "transaction_id": str(t.transaction_id) if t.transaction_id else None,
            "method": t.method,
            "gateway_reference": getattr(t, "gateway_reference", None),
            "created_at": t.created_at,
        }
        for t in txns
    ]


# ─── Exchange rates ───────────────────────────────────────────────────────────

@router.get("/exchange-rates")
def get_exchange_rates(db: Session = Depends(get_db)):
    """Returns all active admin-controlled exchange rates."""
    rates = db.query(ExchangeRate).filter(ExchangeRate.is_active == True).all()
    return [
        {
            "from": r.from_currency.code,
            "to": r.to_currency.code,
            "rate": r.rate,
            "spread_percent": r.spread_percent,
            "effective_rate": round(r.rate * (1 + r.spread_percent / 100), 8),
            "updated_at": r.updated_at,
        }
        for r in rates
        if r.from_currency and r.to_currency
    ]


@router.get("/rate")
def get_rate(
    from_currency: str,
    to_currency: str,
    db: Session = Depends(get_db),
):
    """Get exchange rate for a specific pair (spread included)."""
    rate = _get_exchange_rate(db, from_currency.upper(), to_currency.upper())
    return {
        "from_currency": from_currency.upper(),
        "to_currency": to_currency.upper(),
        "rate": rate,
        "note": "Rate includes platform spread.",
    }


# ─── Deposit ──────────────────────────────────────────────────────────────────

@router.post("/deposit/initiate")
def initiate_deposit_endpoint(
    payload: DepositRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Step 1 of deposit: returns payment gateway redirect URL / client_secret.
    The wallet is NOT credited until /deposit/verify is called.
    """
    if payload.amount <= 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_AMOUNT", "message": "Deposit amount must be greater than zero."},
        )

    if payload.amount < 1:
        raise HTTPException(
            status_code=422,
            detail={"error": "AMOUNT_TOO_SMALL", "message": "Minimum deposit amount is 1.00."},
        )

    currency_upper = payload.currency.upper()
    active_currency = db.query(Currency).filter(
        Currency.code == currency_upper, Currency.is_active == True
    ).first()
    if not active_currency:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "CURRENCY_NOT_SUPPORTED",
                "message": f"'{currency_upper}' is not currently supported. Check /wallet/currencies for available currencies.",
            },
        )

    callback_url = f"{settings.FRONTEND_URL}/wallet/deposit/callback"

    try:
        result = initiate_deposit(
            user=current_user,
            amount=payload.amount,
            currency=currency_upper,
            callback_url=callback_url,
            db=db,
            metadata={"user_id": str(current_user.id)},
        )
    except (PaystackError, StripeError, FlutterwaveError) as e:
        raise HTTPException(
            status_code=502,
            detail={"error": "GATEWAY_ERROR", "message": str(e)},
        )

    # Record pending transaction
    txn = WalletTransaction(
        user_id=current_user.id,
        type="deposit",
        amount=payload.amount,
        currency=currency_upper,
        status="pending",
        description=f"{result['gateway'].title()} deposit",
        method=result["gateway"],
        reference=result.get("reference", ""),
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)

    return {
        **result,
        "wallet_transaction_id": str(txn.id),
        "amount": payload.amount,
        "currency": currency_upper,
    }


@router.post("/deposit/verify")
def verify_deposit_endpoint(
    gateway: str,
    reference: str,
    wallet_transaction_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Step 2 of deposit: verify with gateway and credit wallet on success.
    Idempotent — safe to call multiple times with same reference.
    """
    # Check for existing completed transaction (idempotency)
    if wallet_transaction_id:
        existing = db.query(WalletTransaction).filter(
            WalletTransaction.id == wallet_transaction_id,
            WalletTransaction.user_id == current_user.id,
        ).first()
        if existing and existing.status == "completed":
            return {"message": "Deposit already credited.", "status": "completed", "amount": existing.amount}

    try:
        result = verify_deposit(gateway=gateway, reference=reference, db=db)
    except (PaystackError, StripeError, FlutterwaveError) as e:
        raise HTTPException(
            status_code=502,
            detail={"error": "GATEWAY_VERIFICATION_FAILED", "message": str(e)},
        )
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": "UNKNOWN_GATEWAY", "message": str(e)},
        )

    if not result["success"]:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "PAYMENT_NOT_SUCCESSFUL",
                "message": f"Payment was not completed. Gateway status: {result.get('status', 'unknown')}. Please retry or use a different payment method.",
            },
        )

    # Credit wallet idempotently (reuses pending tx by id/reference)
    txn, balance = _apply_verified_deposit(
        db=db,
        user_id=current_user.id,
        gateway=gateway,
        reference=result["reference"],
        amount=result["amount"],
        currency=result["currency"],
        wallet_transaction_id=wallet_transaction_id,
    )

    create_notification(
        db, current_user.id,
        "Deposit Successful",
        f"Your deposit of {result['currency']} {result['amount']:.2f} was successful.",
        "payment",
    )

    return {
        "message": "Deposit successful. Wallet credited.",
        "amount": result["amount"],
        "currency": result["currency"],
        "new_balance": balance.amount,
        "wallet_transaction_id": str(txn.id),
    }


@router.post("/deposit/reconcile")
def reconcile_deposit_endpoint(
    gateway: str,
    reference: str,
    wallet_transaction_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Manual recovery endpoint: verify gateway payment and reconcile pending wallet tx.
    Useful when the frontend callback was interrupted.
    """
    if current_user.wallet_frozen:
        raise HTTPException(status_code=403, detail={"error": "WALLET_FROZEN", "message": "Your wallet has been frozen. Contact support."})

    try:
        result = verify_deposit(gateway=gateway, reference=reference, db=db)
    except (PaystackError, StripeError, FlutterwaveError) as e:
        raise HTTPException(status_code=502, detail={"error": "GATEWAY_VERIFICATION_FAILED", "message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"error": "UNKNOWN_GATEWAY", "message": str(e)})

    if not result["success"]:
        raise HTTPException(
            status_code=422,
            detail={"error": "PAYMENT_NOT_SUCCESSFUL", "message": f"Gateway status is not successful for reference {reference}."},
        )

    txn, balance = _apply_verified_deposit(
        db=db,
        user_id=current_user.id,
        gateway=gateway,
        reference=result["reference"],
        amount=result["amount"],
        currency=result["currency"],
        wallet_transaction_id=wallet_transaction_id,
    )
    return {
        "message": "Deposit reconciled successfully.",
        "amount": result["amount"],
        "currency": result["currency"],
        "new_balance": balance.amount,
        "wallet_transaction_id": str(txn.id),
    }


@router.post("/webhooks/paystack")
async def paystack_webhook(
    request: Request,
    x_paystack_signature: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    Paystack webhook receiver (public endpoint).
    Handles successful charge events as fallback when frontend verify isn't called.
    """
    body = await request.body()
    if not x_paystack_signature or not paystack_verify_webhook(body, x_paystack_signature):
        raise HTTPException(status_code=401, detail="Invalid Paystack signature")

    payload = await request.json()
    event = payload.get("event")
    data = payload.get("data") or {}
    if event != "charge.success":
        return {"ok": True, "ignored": event}

    metadata = data.get("metadata") or {}
    user_id = metadata.get("user_id")
    if not user_id:
        return {"ok": True, "ignored": "missing_user_id_metadata"}

    reference = data.get("reference")
    if not reference:
        return {"ok": True, "ignored": "missing_reference"}

    amount_minor = int(data.get("amount") or 0)
    amount = amount_minor / 100.0
    currency = (data.get("currency") or "NGN").upper()

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"ok": True, "ignored": "unknown_user"}
    if user.wallet_frozen:
        return {"ok": True, "ignored": "wallet_frozen"}

    _apply_verified_deposit(
        db=db,
        user_id=user_id,
        gateway="paystack",
        reference=reference,
        amount=amount,
        currency=currency,
    )
    return {"ok": True}


# ─── Withdrawal ───────────────────────────────────────────────────────────────

@router.post("/withdraw")
def withdraw(
    payload: WithdrawRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Request a withdrawal. Funds are moved to pending_amount immediately.
    Admin reviews and processes via the payment gateway.
    """
    # Risk & fraud controls
    if current_user.wallet_frozen:
        raise HTTPException(status_code=403, detail={"error": "WALLET_FROZEN", "message": "Your wallet has been frozen. Contact support@milevault.com."})
    if current_user.withdrawals_blocked:
        raise HTTPException(status_code=403, detail={"error": "WITHDRAWALS_BLOCKED", "message": "Withdrawals are currently blocked on your account. Contact support@milevault.com."})
    if not current_user.is_kyc_verified:
        raise HTTPException(status_code=403, detail={"error": "KYC_REQUIRED", "message": "Identity verification (KYC) is required before withdrawing funds."})
    from datetime import datetime
    if current_user.withdrawal_cooldown_until and datetime.utcnow() < current_user.withdrawal_cooldown_until:
        wait_until = current_user.withdrawal_cooldown_until.strftime("%Y-%m-%d %H:%M UTC")
        raise HTTPException(status_code=429, detail={"error": "COOLDOWN_ACTIVE", "message": f"Withdrawal cooldown active. You can withdraw after {wait_until}."})

    from app.models.user import UserSettings
    import pyotp

    us = db.query(UserSettings).filter(UserSettings.user_id == current_user.id).first()
    if us and us.two_factor_enabled:
        if not payload.otp_code or not str(payload.otp_code).strip():
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "TWO_FACTOR_REQUIRED",
                    "message": "Enter the 6-digit code from your authenticator app to request a withdrawal.",
                },
            )
        if not us.two_factor_secret:
            raise HTTPException(
                status_code=503,
                detail={"error": "TWO_FACTOR_MISCONFIGURED", "message": "Two-factor authentication is enabled but not set up correctly. Contact support."},
            )
        if not pyotp.TOTP(us.two_factor_secret).verify(str(payload.otp_code).strip(), valid_window=1):
            raise HTTPException(
                status_code=401,
                detail={"error": "INVALID_OTP", "message": "Invalid or expired authentication code."},
            )

    if payload.amount <= 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_AMOUNT", "message": "Withdrawal amount must be greater than zero."},
        )

    currency_upper = payload.currency.upper()
    balance = get_or_create_balance(db, current_user.id, currency_upper)

    if balance.amount < payload.amount:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "INSUFFICIENT_BALANCE",
                "message": (
                    f"Your {currency_upper} balance ({balance.amount:.2f}) is insufficient for this withdrawal. "
                    f"Requested: {payload.amount:.2f}."
                ),
            },
        )

    balance.amount -= payload.amount
    balance.pending_amount = (balance.pending_amount or 0) + payload.amount

    if payload.amount >= 5000:
        current_user.risk_score = min(100.0, float(current_user.risk_score or 0) + 1.0)

    bank_note = f" | {payload.bank_details}" if payload.bank_details else ""
    from app.services.reputation_limits import withdrawal_flagged_high_risk

    txn = WalletTransaction(
        user_id=current_user.id,
        type="withdrawal",
        amount=payload.amount,
        currency=currency_upper,
        status="pending",
        description=f"Withdrawal via {payload.method}{bank_note}",
        method=payload.method,
        flagged_high_risk=withdrawal_flagged_high_risk(current_user, float(payload.amount)),
    )
    db.add(txn)
    db.commit()

    create_notification(
        db, current_user.id,
        "Withdrawal Requested",
        f"Withdrawal of {currency_upper} {payload.amount:.2f} is being processed. It typically takes 1–3 business days.",
        "payment",
    )

    return {
        "message": "Withdrawal request submitted successfully.",
        "status": "pending",
        "amount": payload.amount,
        "currency": currency_upper,
        "wallet_transaction_id": str(txn.id),
    }


# ─── Wallet-to-wallet transfer ───────────────────────────────────────────────

@router.post("/transfer")
def transfer_funds(
    payload: TransferRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send funds from your wallet to another MileVault user by email."""
    if payload.amount <= 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_AMOUNT", "message": "Transfer amount must be greater than zero."},
        )

    if payload.recipient_email.lower() == current_user.email.lower():
        raise HTTPException(
            status_code=422,
            detail={"error": "SELF_TRANSFER", "message": "You cannot transfer funds to yourself."},
        )

    currency_upper = payload.currency.upper()

    recipient = db.query(User).filter(
        User.email == payload.recipient_email.lower(),
        User.is_active == True,
    ).first()
    if not recipient:
        raise HTTPException(
            status_code=404,
            detail={"error": "RECIPIENT_NOT_FOUND", "message": "No active MileVault account found with that email address."},
        )

    sender_balance = get_or_create_balance(db, current_user.id, currency_upper)
    if sender_balance.amount < payload.amount:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "INSUFFICIENT_BALANCE",
                "message": f"Your {currency_upper} balance ({sender_balance.amount:.2f}) is insufficient for this transfer.",
            },
        )

    # Debit sender
    sender_balance.amount -= payload.amount

    # Credit recipient
    recipient_balance = get_or_create_balance(db, recipient.id, currency_upper)
    recipient_balance.amount += payload.amount

    note_text = f" — {payload.note}" if payload.note else ""

    sender_txn = WalletTransaction(
        user_id=current_user.id,
        type="transfer_out",
        amount=payload.amount,
        currency=currency_upper,
        status="completed",
        description=f"Transfer to {recipient.email}{note_text}",
        method="internal",
    )
    recipient_txn = WalletTransaction(
        user_id=recipient.id,
        type="transfer_in",
        amount=payload.amount,
        currency=currency_upper,
        status="completed",
        description=f"Transfer from {current_user.email}{note_text}",
        method="internal",
    )
    db.add(sender_txn)
    db.add(recipient_txn)
    db.commit()

    create_notification(
        db, current_user.id,
        "Transfer Sent",
        f"You sent {currency_upper} {payload.amount:.2f} to {recipient.email}.",
        "payment",
    )
    create_notification(
        db, recipient.id,
        "Funds Received",
        f"You received {currency_upper} {payload.amount:.2f} from {current_user.email}.",
        "payment",
    )

    return {
        "message": "Transfer completed successfully.",
        "amount": payload.amount,
        "currency": currency_upper,
        "recipient": recipient.email,
        "sender_new_balance": sender_balance.amount,
    }


# ─── Currency conversion ──────────────────────────────────────────────────────

@router.post("/convert")
def convert_currency(
    payload: ConvertRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from_code = payload.from_currency.upper()
    to_code = payload.to_currency.upper()

    if from_code == to_code:
        raise HTTPException(
            status_code=422,
            detail={"error": "SAME_CURRENCY", "message": "Source and target currencies must be different."},
        )

    if payload.amount <= 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_AMOUNT", "message": "Conversion amount must be greater than zero."},
        )

    from_balance = get_or_create_balance(db, current_user.id, from_code)
    if from_balance.amount < payload.amount:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "INSUFFICIENT_BALANCE",
                "message": (
                    f"Your {from_code} balance ({from_balance.amount:.2f}) is insufficient. "
                    f"Requested: {payload.amount:.2f}."
                ),
            },
        )

    rate = _get_exchange_rate(db, from_code, to_code)
    converted = round(payload.amount * rate, 8)

    from_balance.amount -= payload.amount
    to_balance = get_or_create_balance(db, current_user.id, to_code)
    to_balance.amount += converted

    txn = WalletTransaction(
        user_id=current_user.id,
        type="conversion",
        amount=payload.amount,
        currency=from_code,
        status="completed",
        description=f"Converted {from_code} {payload.amount:.4f} → {to_code} {converted:.4f} @ {rate}",
    )
    db.add(txn)
    db.commit()

    return {
        "message": "Currency converted successfully.",
        "from_currency": from_code,
        "to_currency": to_code,
        "original_amount": payload.amount,
        "converted_amount": converted,
        "rate_applied": rate,
        "note": "Rate includes platform spread.",
    }


# ─── Supported currencies ─────────────────────────────────────────────────────

@router.get("/currencies")
def list_currencies(db: Session = Depends(get_db)):
    """Returns admin-configured active currencies."""
    currencies = db.query(Currency).filter(Currency.is_active == True).order_by(Currency.code).all()
    return [
        {
            "code": c.code,
            "name": c.name,
            "symbol": c.symbol,
            "type": c.type,
            "decimal_places": c.decimal_places,
        }
        for c in currencies
    ]


@router.get("/platform-info")
def platform_info(db: Session = Depends(get_db)):
    """Public endpoint returning platform fee and currency info for the fee calculator."""
    from app.models.currency import PlatformSettings
    from app.services.platform_timeline import (
        get_funding_deadline_days,
        get_auto_release_days,
        get_invite_expiry_days,
        get_stale_activity_warn_days,
    )

    settings = db.query(PlatformSettings).filter(PlatformSettings.id == "default").first()
    return {
        "escrow_fee_percent": settings.escrow_fee_percent if settings else 2.5,
        "min_fee_amount": settings.min_fee_amount if settings else 0,
        "max_fee_amount": settings.max_fee_amount if settings else None,
        "fee_currency": settings.fee_currency if settings else "USD",
        "high_value_checklist_threshold": getattr(settings, "high_value_checklist_threshold", None) if settings else None,
        "funding_deadline_days": get_funding_deadline_days(db),
        "auto_release_days": get_auto_release_days(db),
        "invite_expiry_days": get_invite_expiry_days(db),
        "stale_activity_warn_days": get_stale_activity_warn_days(db),
        "ledger_model": "double_entry",
        "custody_notice": (
            "Customer funds for escrow are segregated in ledger accounts (buyer available / buyer escrow / seller available). "
            "Deposits and payouts are processed through configured payment gateways (e.g. Paystack where enabled). "
            "MileVault does not lend customer balances and does not use customer wallet funds for operational expenses."
        ),
        "payment_gateway_note": (
            "Card and bank funding may be processed by third-party gateways such as Paystack depending on region and currency. "
            "See your receipt and gateway terms for settlement timing."
        ),
    }


@router.get("/gateway")
def get_my_gateway(
    currency: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns which payment gateway will be used for the current user's country + currency."""
    gw = get_gateway_for_country(current_user.country_code or "", currency.upper(), db)
    return {
        "gateway": gw,
        "country_code": current_user.country_code,
        "currency": currency.upper(),
    }
