"""
Crypto payment router.
  Public / user-facing:
    GET  /crypto/networks          — list active networks + deposit addresses
    GET  /crypto/my-addresses      — user's saved withdrawal addresses
    POST /crypto/my-addresses      — save a withdrawal address
    DELETE /crypto/my-addresses/{id} — remove a saved address
    POST /crypto/notify-deposit    — user notifies system of a sent tx hash

  Admin (mounted under /admin/crypto/):
    GET    /admin/crypto/deposit-addresses        — list all configured addresses
    POST   /admin/crypto/deposit-addresses        — add / update an address
    DELETE /admin/crypto/deposit-addresses/{net}  — remove address for a network
    GET    /admin/crypto/pending-deposits         — list detected + unconfirmed deposits
    POST   /admin/crypto/pending-deposits/{id}/approve  — credit user wallet
    POST   /admin/crypto/pending-deposits/{id}/reject   — mark rejected
    GET    /admin/crypto/withdrawal-requests      — crypto withdrawal requests
    POST   /admin/crypto/withdrawal-requests/{id}/approve — approve + mark processed
    POST   /admin/crypto/withdrawal-requests/{id}/reject  — reject
"""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from app.database import get_db
from app.dependencies import get_current_user, get_current_admin
from app.models.user import User
from app.models.crypto import CryptoDepositAddress, CryptoPendingDeposit, UserCryptoAddress, NETWORK_LABELS, NETWORK_CURRENCIES
from app.models.wallet import WalletBalance, WalletTransaction, LedgerEntry
from app.services.notification_service import create_notification

router = APIRouter(prefix="/crypto", tags=["Crypto"])
admin_router = APIRouter(prefix="/admin/crypto", tags=["Admin Crypto"])

SUPPORTED_NETWORKS = list(NETWORK_LABELS.keys())


# ─── User: get deposit addresses ─────────────────────────────────────────────

@router.get("/networks")
def list_crypto_networks(db: Session = Depends(get_db)):
    """Return all active admin-configured deposit addresses."""
    rows = db.query(CryptoDepositAddress).filter(CryptoDepositAddress.is_active == True).all()
    return [
        {
            "network": r.network,
            "label": r.label or NETWORK_LABELS.get(r.network, r.network),
            "address": r.address,
            "currency": NETWORK_CURRENCIES.get(r.network, ""),
            "min_confirmations": r.min_confirmations,
        }
        for r in rows
    ]


# ─── User: saved withdrawal addresses ────────────────────────────────────────

@router.get("/my-addresses")
def list_my_crypto_addresses(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.query(UserCryptoAddress).filter(UserCryptoAddress.user_id == current_user.id).all()
    return [
        {
            "id": r.id,
            "network": r.network,
            "label": r.label or NETWORK_LABELS.get(r.network, r.network),
            "address": r.address,
            "is_default": r.is_default,
        }
        for r in rows
    ]


@router.post("/my-addresses", status_code=201)
def save_crypto_address(
    network: str = Body(...),
    address: str = Body(...),
    label: Optional[str] = Body(None),
    is_default: bool = Body(False),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    network = network.lower().strip()
    if network not in SUPPORTED_NETWORKS:
        raise HTTPException(status_code=422, detail=f"Unsupported network '{network}'.")
    address = address.strip()
    if not address:
        raise HTTPException(status_code=422, detail="Address cannot be empty.")

    existing = db.query(UserCryptoAddress).filter(
        UserCryptoAddress.user_id == current_user.id,
        UserCryptoAddress.network == network,
        UserCryptoAddress.address == address,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="This address is already saved.")

    if is_default:
        db.query(UserCryptoAddress).filter(
            UserCryptoAddress.user_id == current_user.id,
            UserCryptoAddress.network == network,
        ).update({"is_default": False})

    row = UserCryptoAddress(
        user_id=current_user.id,
        network=network,
        address=address,
        label=label,
        is_default=is_default,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "message": "Address saved."}


@router.delete("/my-addresses/{address_id}")
def delete_crypto_address(
    address_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(UserCryptoAddress).filter(
        UserCryptoAddress.id == address_id,
        UserCryptoAddress.user_id == current_user.id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Address not found.")
    db.delete(row)
    db.commit()
    return {"message": "Address removed."}


# ─── User: notify system of a sent deposit ───────────────────────────────────

@router.post("/notify-deposit")
def notify_deposit(
    network: str = Body(...),
    tx_hash: str = Body(...),
    amount_crypto: float = Body(...),
    from_address: Optional[str] = Body(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """User notifies the system they sent a deposit. Admin will verify and approve."""
    network = network.lower().strip()
    tx_hash = tx_hash.strip()
    if network not in SUPPORTED_NETWORKS:
        raise HTTPException(status_code=422, detail=f"Unsupported network '{network}'.")
    if not tx_hash:
        raise HTTPException(status_code=422, detail="Transaction hash is required.")
    if amount_crypto <= 0:
        raise HTTPException(status_code=422, detail="Amount must be greater than zero.")

    existing = db.query(CryptoPendingDeposit).filter(
        CryptoPendingDeposit.tx_hash == tx_hash,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="This transaction hash has already been submitted.")

    dep_addr = db.query(CryptoDepositAddress).filter(
        CryptoDepositAddress.network == network,
        CryptoDepositAddress.is_active == True,
    ).first()

    deposit = CryptoPendingDeposit(
        user_id=current_user.id,
        network=network,
        tx_hash=tx_hash,
        from_address=from_address,
        to_address=dep_addr.address if dep_addr else "",
        amount_crypto=amount_crypto,
        currency=NETWORK_CURRENCIES.get(network, "CRYPTO"),
        status="detected",
    )
    db.add(deposit)
    db.commit()
    return {"message": "Deposit submitted for admin review. Your wallet will be credited once verified."}


# ════════════════════════════════════════════════════════════════════
#  ADMIN endpoints
# ════════════════════════════════════════════════════════════════════

@admin_router.get("/deposit-addresses")
def admin_list_deposit_addresses(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    rows = db.query(CryptoDepositAddress).all()
    return [
        {
            "id": r.id,
            "network": r.network,
            "label": r.label or NETWORK_LABELS.get(r.network, r.network),
            "address": r.address,
            "is_active": r.is_active,
            "min_confirmations": r.min_confirmations,
            "last_scanned_at": r.last_scanned_at.isoformat() if r.last_scanned_at else None,
            "last_scanned_cursor": r.last_scanned_cursor,
        }
        for r in rows
    ]


@admin_router.post("/deposit-addresses", status_code=201)
def admin_set_deposit_address(
    network: str = Body(...),
    address: str = Body(...),
    label: Optional[str] = Body(None),
    min_confirmations: int = Body(1),
    is_active: bool = Body(True),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    network = network.lower().strip()
    if network not in SUPPORTED_NETWORKS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported network. Must be one of: {', '.join(SUPPORTED_NETWORKS)}",
        )
    address = address.strip()
    if not address:
        raise HTTPException(status_code=422, detail="Address cannot be empty.")

    existing = db.query(CryptoDepositAddress).filter(CryptoDepositAddress.network == network).first()
    if existing:
        existing.address = address
        existing.label = label or existing.label
        existing.min_confirmations = min_confirmations
        existing.is_active = is_active
        existing.updated_at = datetime.utcnow()
        db.commit()
        return {"message": f"{network} deposit address updated.", "id": existing.id}

    row = CryptoDepositAddress(
        network=network,
        address=address,
        label=label or NETWORK_LABELS.get(network, network),
        min_confirmations=min_confirmations,
        is_active=is_active,
        created_by=str(admin.id),
    )
    db.add(row)
    db.commit()
    return {"message": f"{network} deposit address configured.", "id": row.id}


@admin_router.delete("/deposit-addresses/{network}")
def admin_delete_deposit_address(
    network: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    row = db.query(CryptoDepositAddress).filter(CryptoDepositAddress.network == network).first()
    if not row:
        raise HTTPException(status_code=404, detail="Network not found.")
    db.delete(row)
    db.commit()
    return {"message": f"{network} deposit address removed."}


@admin_router.get("/pending-deposits")
def admin_list_pending_deposits(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    q = db.query(CryptoPendingDeposit)
    if status:
        q = q.filter(CryptoPendingDeposit.status == status)
    else:
        q = q.filter(CryptoPendingDeposit.status.in_(["detected", "confirmed"]))
    rows = q.order_by(CryptoPendingDeposit.detected_at.desc()).all()

    results = []
    for r in rows:
        user = db.query(User).filter(User.id == r.user_id).first() if r.user_id else None
        results.append({
            "id": r.id,
            "network": r.network,
            "network_label": NETWORK_LABELS.get(r.network, r.network),
            "tx_hash": r.tx_hash,
            "from_address": r.from_address,
            "to_address": r.to_address,
            "amount_crypto": r.amount_crypto,
            "currency": r.currency,
            "fiat_currency": r.fiat_currency,
            "fiat_amount": r.fiat_amount,
            "status": r.status,
            "confirmations": r.confirmations,
            "detected_at": r.detected_at.isoformat() if r.detected_at else None,
            "user_id": r.user_id,
            "user_email": user.email if user else None,
            "user_name": f"{user.first_name} {user.last_name}" if user else None,
        })
    return results


@admin_router.post("/pending-deposits/{deposit_id}/approve")
def admin_approve_deposit(
    deposit_id: str,
    user_id: str = Body(...),
    fiat_amount: float = Body(...),
    fiat_currency: str = Body("NGN"),
    notes: Optional[str] = Body(None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Credit user wallet with the fiat equivalent of the crypto deposit."""
    deposit = db.query(CryptoPendingDeposit).filter(CryptoPendingDeposit.id == deposit_id).first()
    if not deposit:
        raise HTTPException(status_code=404, detail="Deposit not found.")
    if deposit.status in ("approved", "rejected"):
        raise HTTPException(status_code=409, detail=f"Deposit is already {deposit.status}.")
    if fiat_amount <= 0:
        raise HTTPException(status_code=422, detail="Fiat amount must be greater than zero.")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    currency = fiat_currency.upper()
    balance = db.query(WalletBalance).filter(
        WalletBalance.user_id == user_id,
        WalletBalance.currency == currency,
    ).first()
    if not balance:
        balance = WalletBalance(user_id=user_id, currency=currency, amount=0.0)
        db.add(balance)
        db.flush()

    balance.amount = round(balance.amount + fiat_amount, 2)

    db.add(WalletTransaction(
        user_id=user_id,
        type="deposit",
        amount=fiat_amount,
        currency=currency,
        status="completed",
        description=f"Crypto deposit: {deposit.amount_crypto} {deposit.currency} ({deposit.network.upper()}) — TX: {deposit.tx_hash[:16]}…",
        method=f"crypto_{deposit.network}",
    ))

    db.add(LedgerEntry(
        credit_user_id=user_id,
        debit_account="platform",
        credit_account="available",
        amount=fiat_amount,
        currency=currency,
        reference_type="crypto_deposit",
        reference_id=deposit.id,
        description=f"{deposit.amount_crypto} {deposit.currency} deposit approved",
    ))

    deposit.user_id = user_id
    deposit.status = "approved"
    deposit.fiat_amount = fiat_amount
    deposit.fiat_currency = currency
    deposit.approved_at = datetime.utcnow()
    deposit.approved_by = str(admin.id)
    deposit.notes = notes

    create_notification(
        db, user_id,
        "Crypto Deposit Approved",
        f"Your {deposit.amount_crypto} {deposit.currency} deposit has been verified and {currency} {fiat_amount:,.2f} credited to your wallet.",
        "payment",
    )
    db.commit()
    return {"message": "Deposit approved and wallet credited.", "credited": fiat_amount, "currency": currency}


@admin_router.post("/pending-deposits/{deposit_id}/reject")
def admin_reject_deposit(
    deposit_id: str,
    reason: str = Body(...),
    user_id: Optional[str] = Body(None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    deposit = db.query(CryptoPendingDeposit).filter(CryptoPendingDeposit.id == deposit_id).first()
    if not deposit:
        raise HTTPException(status_code=404, detail="Deposit not found.")
    if deposit.status in ("approved", "rejected"):
        raise HTTPException(status_code=409, detail=f"Deposit is already {deposit.status}.")

    deposit.status = "rejected"
    deposit.rejection_reason = reason
    deposit.approved_by = str(admin.id)
    deposit.approved_at = datetime.utcnow()

    uid = user_id or deposit.user_id
    if uid:
        create_notification(
            db, uid,
            "Crypto Deposit Rejected",
            f"Your deposit of {deposit.amount_crypto} {deposit.currency} (TX: {deposit.tx_hash[:16]}…) was rejected. Reason: {reason}",
            "payment",
        )
    db.commit()
    return {"message": "Deposit rejected."}


@admin_router.get("/withdrawal-requests")
def admin_list_crypto_withdrawals(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Crypto withdrawal requests, filterable by status."""
    q = db.query(WalletTransaction).filter(
        WalletTransaction.type == "withdrawal",
        WalletTransaction.method.like("crypto_%"),
    )
    if status:
        q = q.filter(WalletTransaction.status == status)
    else:
        q = q.filter(WalletTransaction.status == "pending")
    rows = q.order_by(WalletTransaction.created_at.desc()).all()

    results = []
    for r in rows:
        user = db.query(User).filter(User.id == r.user_id).first()
        crypto_addr = db.query(UserCryptoAddress).filter(
            UserCryptoAddress.user_id == r.user_id,
            UserCryptoAddress.network == (r.method or "").replace("crypto_", ""),
        ).first()
        results.append({
            "id": str(r.id),
            "user_id": r.user_id,
            "user_email": user.email if user else None,
            "user_name": f"{user.first_name} {user.last_name}" if user else None,
            "amount": r.amount,
            "currency": r.currency,
            "network": (r.method or "").replace("crypto_", ""),
            "network_label": NETWORK_LABELS.get((r.method or "").replace("crypto_", ""), ""),
            "crypto_address": crypto_addr.address if crypto_addr else r.description,
            "description": r.description,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return results


class _WithdrawalApproveBody(BaseModel):
    tx_hash: Optional[str] = None
    notes: Optional[str] = None


@admin_router.post("/withdrawal-requests/{txn_id}/approve")
def admin_approve_crypto_withdrawal(
    txn_id: str,
    body: _WithdrawalApproveBody = Body(default=_WithdrawalApproveBody()),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Mark withdrawal as processed. Funds were sent manually to user's crypto address."""
    txn = db.query(WalletTransaction).filter(
        WalletTransaction.id == txn_id,
        WalletTransaction.type == "withdrawal",
        WalletTransaction.status == "pending",
    ).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Withdrawal request not found or already processed.")

    user_balance = db.query(WalletBalance).filter(
        WalletBalance.user_id == txn.user_id,
        WalletBalance.currency == txn.currency,
    ).first()
    if user_balance:
        user_balance.pending_amount = max(0, round((user_balance.pending_amount or 0) - txn.amount, 2))

    txn.status = "completed"
    extra = []
    if body.tx_hash:
        extra.append(f"TX: {body.tx_hash}")
    if body.notes:
        extra.append(f"Note: {body.notes}")
    if extra:
        txn.description = (txn.description or "") + " | " + " | ".join(extra)

    create_notification(
        db, txn.user_id,
        "Crypto Withdrawal Processed",
        f"Your withdrawal of {txn.currency} {txn.amount:,.2f} has been processed and sent to your crypto address.",
        "payment",
    )
    db.commit()
    return {"message": "Withdrawal marked as processed."}


@admin_router.post("/withdrawal-requests/{txn_id}/reject")
def admin_reject_crypto_withdrawal(
    txn_id: str,
    reason: str = Body(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    txn = db.query(WalletTransaction).filter(
        WalletTransaction.id == txn_id,
        WalletTransaction.type == "withdrawal",
        WalletTransaction.status == "pending",
    ).first()
    if not txn:
        raise HTTPException(status_code=404, detail="Withdrawal request not found or already processed.")

    user_balance = db.query(WalletBalance).filter(
        WalletBalance.user_id == txn.user_id,
        WalletBalance.currency == txn.currency,
    ).first()
    if user_balance:
        user_balance.amount = round(user_balance.amount + txn.amount, 2)
        user_balance.pending_amount = max(0, round((user_balance.pending_amount or 0) - txn.amount, 2))

    txn.status = "failed"
    create_notification(
        db, txn.user_id,
        "Crypto Withdrawal Rejected",
        f"Your withdrawal of {txn.currency} {txn.amount:,.2f} was rejected. Funds returned. Reason: {reason}",
        "payment",
    )
    db.add(LedgerEntry(
        credit_user_id=txn.user_id,
        debit_account="pending",
        credit_account="available",
        amount=txn.amount,
        currency=txn.currency,
        reference_type="withdrawal_reversal",
        reference_id=str(txn.id),
        description=f"Crypto withdrawal rejected: {reason[:100]}",
    ))
    db.commit()
    return {"message": "Withdrawal rejected and funds returned to user."}
