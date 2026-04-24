from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy.orm import Session
from typing import List, Optional
from app.database import get_db
from app.schemas.transaction import TransactionCreate, TransactionUpdate, TransactionOut, MilestoneCreate, MilestoneUpdate, MilestoneOut
from app.models.transaction import Transaction, Milestone
from app.models.user import User
from app.models.wallet import WalletBalance, WalletTransaction, LedgerEntry
from app.dependencies import get_current_user
from app.services.notification_service import create_notification

AUTO_RELEASE_DAYS = 5  # buyer inactivity window

router = APIRouter(prefix="/transactions", tags=["transactions"])


def transaction_to_dict(tx: Transaction) -> dict:
    seller = tx.seller
    buyer = tx.buyer
    return {
        "id": tx.id,
        "title": tx.title,
        "description": tx.description,
        "amount": tx.amount,
        "currency": tx.currency,
        "type": tx.type,
        "status": tx.status,
        "buyer_id": tx.buyer_id,
        "seller_id": tx.seller_id,
        "buyer": {
            "id": buyer.id,
            "first_name": buyer.first_name,
            "last_name": buyer.last_name,
            "name": buyer.first_name + ' ' + buyer.last_name,
            "email": buyer.email,
            "role": buyer.role,
            "avatar_url": buyer.avatar_url,
            "is_kyc_verified": buyer.is_kyc_verified,
            "created_at": buyer.created_at,
        } if buyer else None,
        "seller": {
            "id": seller.id,
            "first_name": seller.first_name,
            "last_name": seller.last_name,
            "name": seller.first_name + ' ' + seller.last_name,
            "email": seller.email,
            "role": seller.role,
            "avatar_url": seller.avatar_url,
            "is_kyc_verified": seller.is_kyc_verified,
            "created_at": seller.created_at,
        } if seller else None,
        "supporting_url": tx.supporting_url,
        "contract_signed": tx.contract_signed,
        "created_at": tx.created_at,
        "updated_at": tx.updated_at,
        "additional_details": {
            "project_url": tx.project_url,
            "expected_delivery_date": tx.expected_completion_date.isoformat() if tx.expected_completion_date else None,
            "milestones_count": tx.milestones_count,
            "completed_milestones": tx.completed_milestones,
            "notes": tx.notes,
            "service_fee_payment": tx.service_fee_payment,
            "service_fee_ratio": {"buyer": tx.buyer_fee_ratio, "seller": tx.seller_fee_ratio},
        },
        "milestones": [
            {
                "id": m.id,
                "transaction_id": m.transaction_id,
                "title": m.title,
                "description": m.description,
                "amount": m.amount,
                "currency": m.currency,
                "status": m.status,
                "due_date": m.due_date,
                "completed_date": m.completed_date,
                "expectations": m.expectations,
                "feedback": m.feedback,
                "percentage_of_total": m.percentage_of_total,
                "attachments": m.attachments or [],
                "supporting_documents": m.supporting_documents or [],
                "created_at": m.created_at,
            }
            for m in tx.milestones
        ],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_transaction(
    payload: TransactionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Find seller by email if provided
    seller = None
    if payload.counterparty_email:
        seller = db.query(User).filter(User.email == payload.counterparty_email).first()

    tx = Transaction(
        title=payload.title,
        description=payload.description,
        amount=payload.amount,
        currency=payload.currency,
        type=payload.type,
        buyer_id=current_user.id,
        seller_id=seller.id if seller else None,
        counterparty_email=payload.counterparty_email,
        expected_completion_date=payload.expected_completion_date,
        service_fee_payment=payload.service_fee_payment,
        buyer_fee_ratio=payload.buyer_fee_ratio,
        seller_fee_ratio=payload.seller_fee_ratio,
        notes=payload.notes,
        supporting_url=payload.supporting_url,
        status="pending_approval",
        milestones_count=len(payload.milestones or []),
    )
    db.add(tx)
    db.flush()

    for m in (payload.milestones or []):
        milestone = Milestone(
            transaction_id=tx.id,
            title=m.title,
            description=m.description,
            amount=m.amount,
            currency=m.currency or payload.currency,
            due_date=m.due_date,
            expectations=m.expectations,
            percentage_of_total=m.percentage_of_total,
            attachments=m.attachments or [],
            supporting_documents=m.supporting_documents or [],
        )
        db.add(milestone)

    db.commit()
    db.refresh(tx)

    if seller:
        create_notification(
            db, seller.id,
            "New Transaction Invitation",
            f"{current_user.first_name + ' ' + current_user.last_name} invited you to a transaction: {tx.title}",
            "transaction",
            related_item_id=tx.id,
            related_item_type="transaction",
        )

    return transaction_to_dict(tx)


@router.get("")
def list_transactions(
    status: Optional[str] = None,
    type: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Transaction).filter(
        (Transaction.buyer_id == current_user.id) | (Transaction.seller_id == current_user.id)
    )
    if status:
        query = query.filter(Transaction.status == status)
    if type:
        query = query.filter(Transaction.type == type)
    transactions = query.order_by(Transaction.updated_at.desc()).all()
    return [transaction_to_dict(tx) for tx in transactions]


@router.get("/{transaction_id}")
def get_transaction(
    transaction_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id and tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return transaction_to_dict(tx)


@router.put("/{transaction_id}")
def update_transaction(
    transaction_id: str,
    payload: TransactionUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id and tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(tx, field, value)
    db.commit()
    db.refresh(tx)

    other_user_id = tx.seller_id if tx.buyer_id == current_user.id else tx.buyer_id
    if other_user_id and payload.status:
        create_notification(
            db, other_user_id,
            "Transaction Updated",
            f"Transaction '{tx.title}' status changed to {payload.status}",
            "transaction",
            related_item_id=tx.id,
            related_item_type="transaction",
        )

    return transaction_to_dict(tx)


@router.delete("/{transaction_id}")
def cancel_transaction(
    transaction_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can cancel")

    tx.status = "cancelled"
    db.commit()
    return {"message": "Transaction cancelled"}


# Milestone endpoints
@router.get("/{transaction_id}/milestones")
def get_milestones(
    transaction_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id and tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    return [
        {
            "id": m.id,
            "transaction_id": m.transaction_id,
            "title": m.title,
            "description": m.description,
            "amount": m.amount,
            "currency": m.currency,
            "status": m.status,
            "due_date": m.due_date,
            "completed_date": m.completed_date,
            "expectations": m.expectations,
            "feedback": m.feedback,
            "percentage_of_total": m.percentage_of_total,
            "attachments": m.attachments or [],
            "supporting_documents": m.supporting_documents or [],
            "created_at": m.created_at,
        }
        for m in tx.milestones
    ]


@router.post("/{transaction_id}/milestones", status_code=status.HTTP_201_CREATED)
def create_milestone(
    transaction_id: str,
    payload: MilestoneCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only buyer can add milestones")

    milestone = Milestone(
        transaction_id=transaction_id,
        title=payload.title,
        description=payload.description,
        amount=payload.amount,
        currency=payload.currency or tx.currency,
        due_date=payload.due_date,
        expectations=payload.expectations,
        percentage_of_total=payload.percentage_of_total,
        attachments=payload.attachments or [],
        supporting_documents=payload.supporting_documents or [],
    )
    db.add(milestone)
    tx.milestones_count = (tx.milestones_count or 0) + 1
    db.commit()
    db.refresh(milestone)
    return milestone


@router.put("/{transaction_id}/milestones/{milestone_id}")
def update_milestone(
    transaction_id: str,
    milestone_id: str,
    payload: MilestoneUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id and tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    milestone = db.query(Milestone).filter(
        Milestone.id == milestone_id, Milestone.transaction_id == transaction_id
    ).first()
    if not milestone:
        raise HTTPException(status_code=404, detail="Milestone not found")

    prev_status = milestone.status
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(milestone, field, value)

    if payload.status == "completed" and prev_status != "completed":
        milestone.completed_date = datetime.utcnow()
        tx.completed_milestones = (tx.completed_milestones or 0) + 1

    db.commit()
    db.refresh(milestone)
    return milestone


# ══════════════════════════════════════════════════════════════════
#  SELLER: Accept transaction invite
# ══════════════════════════════════════════════════════════════════

@router.post("/{transaction_id}/accept")
def accept_transaction(
    transaction_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the invited seller can accept")
    if tx.status not in ("pending_acceptance", "pending_approval"):
        raise HTTPException(status_code=409, detail=f"Cannot accept transaction in status '{tx.status}'")

    tx.status = "funding_in_progress"
    create_notification(
        db, tx.buyer_id,
        "Seller Accepted Your Transaction",
        f"{current_user.first_name} accepted '{tx.title}'. Please fund the milestones to begin.",
        "transaction", related_item_id=tx.id, related_item_type="transaction",
    )
    db.commit()
    return {"message": "Transaction accepted. Buyer can now fund milestones."}


@router.post("/{transaction_id}/decline")
def decline_transaction(
    transaction_id: str,
    reason: Optional[str] = Body(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the invited seller can decline")

    tx.status = "cancelled"
    create_notification(
        db, tx.buyer_id,
        "Transaction Declined",
        f"{current_user.first_name} declined '{tx.title}'." + (f" Reason: {reason}" if reason else ""),
        "transaction", related_item_id=tx.id, related_item_type="transaction",
    )
    db.commit()
    return {"message": "Transaction declined."}


# ══════════════════════════════════════════════════════════════════
#  BUYER: Fund a milestone (moves funds from available → escrow)
# ══════════════════════════════════════════════════════════════════

@router.post("/{transaction_id}/milestones/{milestone_id}/fund")
def fund_milestone(
    transaction_id: str,
    milestone_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Buyer funds a milestone: deducts from wallet available balance,
    adds to wallet escrow balance. Milestone becomes 'funded'.
    Transaction activates when ≥1 milestone is funded.
    """
    if current_user.wallet_frozen:
        raise HTTPException(status_code=403, detail={"error": "WALLET_FROZEN", "message": "Your wallet has been frozen. Contact support."})

    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can fund milestones")
    if tx.status not in ("funding_in_progress", "partially_funded", "active", "approved"):
        raise HTTPException(status_code=409, detail=f"Cannot fund milestones in transaction status '{tx.status}'")

    milestone = db.query(Milestone).filter(
        Milestone.id == milestone_id, Milestone.transaction_id == transaction_id
    ).first()
    if not milestone:
        raise HTTPException(status_code=404, detail="Milestone not found")
    if milestone.is_funded:
        raise HTTPException(status_code=409, detail="Milestone is already funded")

    currency = milestone.currency or tx.currency
    amount = milestone.amount

    # Check buyer wallet balance
    balance = db.query(WalletBalance).filter(
        WalletBalance.user_id == current_user.id,
        WalletBalance.currency == currency,
    ).first()
    if not balance or balance.amount < amount:
        available = balance.amount if balance else 0.0
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INSUFFICIENT_FUNDS",
                "message": f"Insufficient balance. You have {currency} {available:.2f}, need {currency} {amount:.2f}. Please deposit funds first.",
            },
        )

    # Deduct from available, add to escrow
    balance.amount = round(balance.amount - amount, 8)
    balance.escrow_amount = round((balance.escrow_amount or 0) + amount, 8)

    # Mark milestone as funded
    milestone.funded_amount = amount
    milestone.is_funded = True
    milestone.status = "funded"
    milestone.auto_release_at = datetime.utcnow() + timedelta(days=AUTO_RELEASE_DAYS)

    # Ledger: buyer available → buyer escrow
    db.add(LedgerEntry(
        debit_user_id=current_user.id,
        credit_user_id=current_user.id,
        debit_account="available",
        credit_account="escrow",
        amount=amount,
        currency=currency,
        reference_type="milestone_fund",
        reference_id=milestone_id,
        description=f"Escrow lock for milestone '{milestone.title}'",
    ))

    # Wallet transaction audit trail
    db.add(WalletTransaction(
        user_id=current_user.id,
        type="escrow_lock",
        amount=amount,
        currency=currency,
        status="completed",
        transaction_id=transaction_id,
        milestone_id=milestone_id,
        description=f"Escrow for milestone: {milestone.title}",
    ))

    # Activate transaction if first funded milestone
    tx.funded_milestones = (tx.funded_milestones or 0) + 1
    if tx.status in ("funding_in_progress", "partially_funded", "approved"):
        tx.status = "active"

    create_notification(
        db, tx.seller_id or "",
        "Milestone Funded — Work Can Begin",
        f"'{milestone.title}' in '{tx.title}' has been funded ({currency} {amount:.2f}). You can start work.",
        "transaction", related_item_id=tx.id, related_item_type="transaction",
    )

    # Schedule auto-release task
    try:
        from app.services.tasks import auto_release_milestone
        auto_release_milestone.apply_async(
            args=[milestone_id],
            countdown=AUTO_RELEASE_DAYS * 86400,
        )
    except Exception:
        pass

    db.commit()
    return {
        "message": f"Milestone '{milestone.title}' funded. Funds locked in escrow.",
        "escrow_amount": amount,
        "currency": currency,
    }


# ══════════════════════════════════════════════════════════════════
#  SELLER: Submit delivery
# ══════════════════════════════════════════════════════════════════

@router.post("/{transaction_id}/milestones/{milestone_id}/deliver")
def submit_delivery(
    transaction_id: str,
    milestone_id: str,
    delivery_note: Optional[str] = Body(None),
    delivery_attachments: Optional[list] = Body(default=[]),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Seller submits delivery. Milestone moves to 'delivered'. Buyer has AUTO_RELEASE_DAYS to review."""
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the seller can submit delivery")

    milestone = db.query(Milestone).filter(
        Milestone.id == milestone_id, Milestone.transaction_id == transaction_id
    ).first()
    if not milestone:
        raise HTTPException(status_code=404, detail="Milestone not found")
    if not milestone.is_funded:
        raise HTTPException(status_code=409, detail="Cannot deliver an unfunded milestone")
    if milestone.status not in ("funded", "in_progress", "revision_requested"):
        raise HTTPException(status_code=409, detail=f"Cannot deliver milestone in status '{milestone.status}'")

    now = datetime.utcnow()
    milestone.status = "delivered"
    milestone.delivered_at = now
    milestone.delivery_note = delivery_note
    milestone.delivery_attachments = delivery_attachments or []
    # Reset auto-release window from delivery time
    milestone.auto_release_at = now + timedelta(days=AUTO_RELEASE_DAYS)

    if tx.status == "active":
        tx.status = "in_progress"

    create_notification(
        db, tx.buyer_id,
        "Delivery Submitted — Action Required",
        f"'{milestone.title}' has been delivered by {current_user.first_name}. Review and approve or request a revision within {AUTO_RELEASE_DAYS} days.",
        "transaction", related_item_id=tx.id, related_item_type="transaction",
    )
    db.commit()
    return {"message": "Delivery submitted. Awaiting buyer approval."}


# ══════════════════════════════════════════════════════════════════
#  BUYER: Approve delivery → release escrow to seller
# ══════════════════════════════════════════════════════════════════

@router.post("/{transaction_id}/milestones/{milestone_id}/approve")
def approve_milestone(
    transaction_id: str,
    milestone_id: str,
    feedback: Optional[str] = Body(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Buyer approves delivery. Escrow released instantly to seller available balance."""
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can approve milestones")

    milestone = db.query(Milestone).filter(
        Milestone.id == milestone_id, Milestone.transaction_id == transaction_id
    ).first()
    if not milestone:
        raise HTTPException(status_code=404, detail="Milestone not found")
    if milestone.status not in ("delivered", "funded", "in_progress"):
        raise HTTPException(status_code=409, detail=f"Cannot approve milestone in status '{milestone.status}'")

    _release_milestone_escrow(db, tx, milestone, feedback)
    db.commit()
    return {"message": "Milestone approved. Funds released to seller.", "amount": milestone.amount}


def _release_milestone_escrow(db: Session, tx: Transaction, milestone: Milestone, feedback: str = None):
    """Internal helper: release milestone escrow to seller. Called by approve and auto-release."""
    currency = milestone.currency or tx.currency
    amount = milestone.funded_amount or milestone.amount

    # Deduct from buyer escrow
    buyer_balance = db.query(WalletBalance).filter(
        WalletBalance.user_id == tx.buyer_id,
        WalletBalance.currency == currency,
    ).first()
    if buyer_balance:
        buyer_balance.escrow_amount = max(0, round((buyer_balance.escrow_amount or 0) - amount, 8))

    # Credit seller available
    seller_balance = db.query(WalletBalance).filter(
        WalletBalance.user_id == tx.seller_id,
        WalletBalance.currency == currency,
    ).first()
    if not seller_balance:
        seller_balance = WalletBalance(user_id=tx.seller_id, currency=currency, amount=0.0)
        db.add(seller_balance)
    seller_balance.amount = round(seller_balance.amount + amount, 8)

    # Ledger: buyer escrow → seller available
    db.add(LedgerEntry(
        debit_user_id=tx.buyer_id,
        credit_user_id=tx.seller_id,
        debit_account="escrow",
        credit_account="available",
        amount=amount,
        currency=currency,
        reference_type="milestone_release",
        reference_id=milestone.id,
        description=f"Release for milestone '{milestone.title}'",
    ))

    # Wallet transaction audit
    db.add(WalletTransaction(
        user_id=tx.seller_id,
        type="escrow_release",
        amount=amount,
        currency=currency,
        status="completed",
        transaction_id=tx.id,
        milestone_id=milestone.id,
        description=f"Payment for milestone: {milestone.title}",
    ))

    milestone.status = "completed"
    milestone.completed_date = datetime.utcnow()
    if feedback:
        milestone.feedback = feedback

    tx.completed_milestones = (tx.completed_milestones or 0) + 1

    # Update seller reputation
    _update_reputation(db, tx.seller_id)
    _update_reputation(db, tx.buyer_id)

    # Check if all milestones complete
    all_milestones = db.query(Milestone).filter(Milestone.transaction_id == tx.id).all()
    if all(m.status == "completed" for m in all_milestones):
        tx.status = "completed"
        # Update total volume
        seller = db.query(User).filter(User.id == tx.seller_id).first()
        if seller:
            seller.total_volume = round((seller.total_volume or 0) + amount, 2)

    create_notification(
        db, tx.seller_id,
        "Payment Released!",
        f"Your payment of {currency} {amount:.2f} for '{milestone.title}' has been released to your wallet.",
        "transaction", related_item_id=tx.id, related_item_type="transaction",
    )


def _update_reputation(db: Session, user_id: str):
    """Recalculate completion_rate and dispute_rate for a user."""
    from app.models.dispute import Dispute
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return
    total = db.query(Transaction).filter(
        (Transaction.buyer_id == user_id) | (Transaction.seller_id == user_id)
    ).count()
    completed = db.query(Transaction).filter(
        (Transaction.buyer_id == user_id) | (Transaction.seller_id == user_id),
        Transaction.status == "completed",
    ).count()
    disputed = db.query(Dispute).filter(Dispute.raised_by == user_id).count()
    user.completion_rate = round((completed / total * 100) if total else 0.0, 1)
    user.dispute_rate = round((disputed / total * 100) if total else 0.0, 1)


# ══════════════════════════════════════════════════════════════════
#  BUYER: Request revision
# ══════════════════════════════════════════════════════════════════

@router.post("/{transaction_id}/milestones/{milestone_id}/revision")
def request_revision(
    transaction_id: str,
    milestone_id: str,
    note: str = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Buyer requests a revision. Milestone goes back to seller without releasing funds."""
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can request revisions")

    milestone = db.query(Milestone).filter(
        Milestone.id == milestone_id, Milestone.transaction_id == transaction_id
    ).first()
    if not milestone:
        raise HTTPException(status_code=404, detail="Milestone not found")
    if milestone.status != "delivered":
        raise HTTPException(status_code=409, detail="Can only request revision on delivered milestones")

    milestone.status = "revision_requested"
    milestone.revision_note = note
    # Reset auto-release window — seller needs to re-deliver
    milestone.auto_release_at = None

    create_notification(
        db, tx.seller_id,
        "Revision Requested",
        f"Buyer requested changes on '{milestone.title}': {note[:200]}",
        "transaction", related_item_id=tx.id, related_item_type="transaction",
    )
    db.commit()
    return {"message": "Revision requested. Seller will re-submit delivery."}
