from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import Optional, Any, Dict
from app.database import get_db
from app.schemas.transaction import (
    TransactionCreate,
    TransactionUpdate,
    TransactionOut,
    MilestoneCreate,
    MilestoneUpdate,
    MilestoneOut,
    FundMilestoneBody,
    DeliverySubmit,
    ApproveMilestoneBody,
    InvalidDeliveryReport,
)
from app.models.transaction import Transaction, Milestone
from app.models.user import User
from app.models.wallet import WalletBalance, WalletTransaction, LedgerEntry
from app.dependencies import get_current_user
from app.services.notification_service import create_notification
router = APIRouter(prefix="/transactions", tags=["transactions"])


def milestone_to_public_dict(m: Milestone) -> Dict[str, Any]:
    return {
        "id": m.id,
        "transaction_id": m.transaction_id,
        "title": m.title,
        "description": m.description,
        "amount": m.amount,
        "currency": m.currency,
        "status": m.status,
        "due_date": m.due_date,
        "completed_date": m.completed_date,
        "delivered_at": m.delivered_at,
        "auto_release_at": m.auto_release_at,
        "funded_amount": m.funded_amount or 0,
        "is_funded": bool(m.is_funded),
        "expectations": m.expectations,
        "feedback": m.feedback,
        "revision_note": m.revision_note,
        "delivery_title": getattr(m, "delivery_title", None),
        "delivery_note": m.delivery_note,
        "delivery_attachments": m.delivery_attachments or [],
        "delivery_external_links": getattr(m, "delivery_external_links", None) or [],
        "delivery_version_notes": getattr(m, "delivery_version_notes", None),
        "invalid_delivery_reported": bool(getattr(m, "invalid_delivery_reported", False)),
        "invalid_delivery_report_note": getattr(m, "invalid_delivery_report_note", None),
        "milestone_action_logs": getattr(m, "milestone_action_logs", None) or [],
        "percentage_of_total": m.percentage_of_total,
        "attachments": m.attachments or [],
        "supporting_documents": m.supporting_documents or [],
        "created_at": m.created_at,
    }


def _append_milestone_log(milestone: Milestone, user_id: str, action: str, detail: Optional[dict] = None) -> None:
    logs = list(milestone.milestone_action_logs or [])
    logs.append(
        {
            "at": datetime.utcnow().isoformat() + "Z",
            "user_id": user_id,
            "action": action,
            "detail": detail or {},
        }
    )
    milestone.milestone_action_logs = logs


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
        "initiated_by_user_id": getattr(tx, "initiated_by_user_id", None),
        "initiated_as": (
            "buyer"
            if not getattr(tx, "initiated_by_user_id", None) or tx.initiated_by_user_id == tx.buyer_id
            else (
                "seller"
                if tx.seller_id and tx.initiated_by_user_id == tx.seller_id
                else None
            )
        ),
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
        "funding_deadline": tx.funding_deadline.isoformat() if tx.funding_deadline else None,
        "locked_exchange_rate": tx.locked_exchange_rate,
        "locked_rate_from": tx.locked_rate_from,
        "locked_rate_to": tx.locked_rate_to,
        "created_at": tx.created_at,
        "updated_at": tx.updated_at,
        "additional_details": {
            "project_url": tx.project_url,
            "expected_delivery_date": tx.expected_completion_date.isoformat() if tx.expected_completion_date else None,
            "milestones_count": tx.milestones_count,
            "funded_milestones": tx.funded_milestones,
            "completed_milestones": tx.completed_milestones,
            "notes": tx.notes,
            "service_fee_payment": tx.service_fee_payment,
            "service_fee_ratio": {"buyer": tx.buyer_fee_ratio, "seller": tx.seller_fee_ratio},
        },
        "milestones": [milestone_to_public_dict(m) for m in tx.milestones],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_transaction(
    payload: TransactionCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    raw_email = (payload.counterparty_email or "").strip()
    email_norm = raw_email.lower() if raw_email else ""
    counterparty = None
    if email_norm:
        counterparty = db.query(User).filter(func.lower(User.email) == email_norm).first()

    if counterparty and counterparty.id == current_user.id:
        raise HTTPException(status_code=422, detail={"message": "You cannot create a transaction with yourself as the counterparty."})

    if payload.initiated_as == "seller":
        if not counterparty:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "When you initiate as the seller, the counterparty must be a registered MileVault user (the buyer who will fund the work).",
                },
            )
        buyer_id = counterparty.id
        seller_id = current_user.id
    else:
        buyer_id = current_user.id
        seller_id = counterparty.id if counterparty else None

    tx = Transaction(
        title=payload.title,
        description=payload.description,
        amount=payload.amount,
        currency=payload.currency,
        type=payload.type,
        buyer_id=buyer_id,
        seller_id=seller_id,
        initiated_by_user_id=current_user.id,
        counterparty_email=raw_email or None,
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

    try:
        from app.routers.wallet import _get_exchange_rate

        cur = (payload.currency or "USD").upper()
        if cur == "USD":
            tx.locked_exchange_rate = 1.0
            tx.locked_rate_from = "USD"
            tx.locked_rate_to = "USD"
        else:
            tx.locked_exchange_rate = _get_exchange_rate(db, cur, "USD")
            tx.locked_rate_from = cur
            tx.locked_rate_to = "USD"
    except HTTPException:
        tx.locked_exchange_rate = None
        tx.locked_rate_from = None
        tx.locked_rate_to = None

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

    invitee_id = seller_id if payload.initiated_as == "buyer" else buyer_id
    if invitee_id and invitee_id != current_user.id:
        create_notification(
            db,
            invitee_id,
            "New transaction invitation",
            f"{current_user.first_name} {current_user.last_name} invited you to a transaction: {tx.title}",
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
    initiator = getattr(tx, "initiated_by_user_id", None)
    if initiator:
        if current_user.id != initiator:
            raise HTTPException(status_code=403, detail="Only the person who sent this invitation can cancel it")
    elif tx.buyer_id != current_user.id:
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

    return [milestone_to_public_dict(m) for m in tx.milestones]


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
#  Counterparty: Accept transaction invite (buyer or seller initiated)
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
    if tx.status not in ("pending_acceptance", "pending_approval"):
        raise HTTPException(status_code=409, detail=f"Cannot accept transaction in status '{tx.status}'")

    initiator = getattr(tx, "initiated_by_user_id", None)
    if initiator:
        if current_user.id == initiator:
            raise HTTPException(status_code=403, detail="You cannot accept your own invitation")
        if current_user.id not in (tx.buyer_id, tx.seller_id):
            raise HTTPException(status_code=403, detail="Access denied")
        if not tx.buyer_id or not tx.seller_id:
            raise HTTPException(
                status_code=409,
                detail="Both buyer and seller must be linked before this invitation can be accepted.",
            )
    else:
        if tx.seller_id != current_user.id:
            raise HTTPException(status_code=403, detail="Only the invited seller can accept")

    from app.services.platform_timeline import get_funding_deadline_days

    tx.status = "funding_in_progress"
    tx.funding_deadline = datetime.utcnow() + timedelta(days=get_funding_deadline_days(db))
    if current_user.id == tx.seller_id:
        create_notification(
            db,
            tx.buyer_id,
            "Invitation accepted",
            f"{current_user.first_name} accepted '{tx.title}'. Please fund the milestones to begin.",
            "transaction",
            related_item_id=tx.id,
            related_item_type="transaction",
        )
    else:
        create_notification(
            db,
            tx.seller_id,
            "Invitation accepted",
            f"{current_user.first_name} accepted '{tx.title}'. They can fund milestones to start escrow for your work.",
            "transaction",
            related_item_id=tx.id,
            related_item_type="transaction",
        )
    db.commit()
    try:
        from app.services.tasks import enforce_funding_deadline

        enforce_funding_deadline.apply_async(args=[tx.id], eta=tx.funding_deadline)
    except Exception:
        pass
    return {"message": "Invitation accepted. Funding can begin when the buyer funds milestones."}


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
    initiator = getattr(tx, "initiated_by_user_id", None)
    if initiator:
        if current_user.id == initiator:
            raise HTTPException(status_code=403, detail="Use cancel to withdraw your invitation")
        if current_user.id not in (tx.buyer_id, tx.seller_id):
            raise HTTPException(status_code=403, detail="Access denied")
    else:
        if tx.seller_id != current_user.id:
            raise HTTPException(status_code=403, detail="Only the invited seller can decline")

    tx.status = "cancelled"
    notify_id = initiator or tx.buyer_id
    create_notification(
        db,
        notify_id,
        "Transaction declined",
        f"{current_user.first_name} declined '{tx.title}'." + (f" Reason: {reason}" if reason else ""),
        "transaction",
        related_item_id=tx.id,
        related_item_type="transaction",
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
    body: FundMilestoneBody = Body(default=FundMilestoneBody()),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Buyer funds a milestone (full or partial). Funds stay inactive until the milestone is 100% funded.
    Transaction becomes Active when ≥1 milestone is fully funded.
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
        raise HTTPException(status_code=409, detail="Milestone is already fully funded")

    currency = milestone.currency or tx.currency
    target = float(milestone.amount)
    already = float(milestone.funded_amount or 0)
    remaining = round(target - already, 8)
    if remaining <= 0:
        raise HTTPException(status_code=409, detail="Milestone is already fully funded")

    pay_raw = body.amount if body.amount is not None else remaining
    pay = round(min(float(pay_raw), remaining), 8)
    if pay <= 0:
        raise HTTPException(status_code=422, detail={"error": "INVALID_AMOUNT", "message": "Funding amount must be greater than zero."})

    balance = db.query(WalletBalance).filter(
        WalletBalance.user_id == current_user.id,
        WalletBalance.currency == currency,
    ).first()
    if not balance or balance.amount < pay:
        available = balance.amount if balance else 0.0
        raise HTTPException(
            status_code=400,
            detail={
                "error": "INSUFFICIENT_FUNDS",
                "message": f"Insufficient balance. You have {currency} {available:.2f}, need {currency} {pay:.2f}. Please deposit funds first.",
            },
        )

    balance.amount = round(balance.amount - pay, 8)
    balance.escrow_amount = round((balance.escrow_amount or 0) + pay, 8)

    new_funded = round(already + pay, 8)
    milestone.funded_amount = new_funded
    became_fully_funded = new_funded >= target - 1e-9
    if became_fully_funded:
        milestone.is_funded = True
        milestone.status = "funded"
        milestone.funded_amount = target
        tx.funded_milestones = (tx.funded_milestones or 0) + 1
    else:
        milestone.is_funded = False
        milestone.status = "partially_funded"

    db.add(
        LedgerEntry(
            debit_user_id=current_user.id,
            credit_user_id=current_user.id,
            debit_account="available",
            credit_account="escrow",
            amount=pay,
            currency=currency,
            reference_type="milestone_fund",
            reference_id=milestone_id,
            description=f"Escrow lock for milestone '{milestone.title}'",
        )
    )
    db.add(
        WalletTransaction(
            user_id=current_user.id,
            type="escrow_lock",
            amount=pay,
            currency=currency,
            status="completed",
            transaction_id=transaction_id,
            milestone_id=milestone_id,
            description=f"Escrow for milestone: {milestone.title}",
        )
    )

    all_ms = db.query(Milestone).filter(Milestone.transaction_id == tx.id).all()
    if any(m.is_funded for m in all_ms):
        tx.status = "active"
    elif any((m.funded_amount or 0) > 0 and not m.is_funded for m in all_ms):
        tx.status = "partially_funded"

    if tx.seller_id:
        create_notification(
            db,
            tx.seller_id,
            "Milestone funding received",
            f"'{milestone.title}' in '{tx.title}': {currency} {pay:.2f} added to escrow"
            + (" — fully funded; you can start work." if became_fully_funded else " (partial — milestone not active until 100% funded)."),
            "transaction",
            related_item_id=tx.id,
            related_item_type="transaction",
        )

    db.commit()
    return {
        "message": f"Milestone '{milestone.title}' funded." + (" Work may begin." if became_fully_funded else " Partial funding recorded."),
        "funded_amount": milestone.funded_amount,
        "is_funded": milestone.is_funded,
        "currency": currency,
    }


# ══════════════════════════════════════════════════════════════════
#  SELLER: Submit delivery
# ══════════════════════════════════════════════════════════════════

@router.post("/{transaction_id}/milestones/{milestone_id}/deliver")
def submit_delivery(
    transaction_id: str,
    milestone_id: str,
    payload: DeliverySubmit,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Seller submits structured delivery proof. Milestone moves to under_review until buyer acts or auto-release."""
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

    from app.services.platform_timeline import get_auto_release_days

    now = datetime.utcnow()
    review_days = get_auto_release_days(db)
    milestone.status = "under_review"
    milestone.delivered_at = now
    milestone.delivery_title = payload.delivery_title.strip()
    milestone.delivery_note = payload.delivery_note.strip()
    milestone.delivery_attachments = [a.strip() for a in payload.delivery_attachments if isinstance(a, str) and a.strip()]
    milestone.delivery_external_links = [u.strip() for u in payload.delivery_external_links if isinstance(u, str) and u.strip()]
    milestone.delivery_version_notes = payload.delivery_version_notes.strip() if payload.delivery_version_notes else None
    milestone.invalid_delivery_reported = False
    milestone.invalid_delivery_report_note = None
    milestone.auto_release_at = now + timedelta(days=review_days)

    if tx.status == "active":
        tx.status = "in_progress"

    _append_milestone_log(
        milestone,
        current_user.id,
        "delivery_submitted",
        {
            "title": milestone.delivery_title,
            "attachments_count": len(milestone.delivery_attachments),
            "links_count": len(milestone.delivery_external_links or []),
        },
    )

    create_notification(
        db,
        tx.buyer_id,
        "Delivery submitted — review required",
        f"'{milestone.title}' is ready for review. Approve, request changes, or report an issue within {review_days} days.",
        "transaction",
        related_item_id=tx.id,
        related_item_type="transaction",
    )

    try:
        from app.services.tasks import auto_release_milestone

        auto_release_milestone.apply_async(args=[milestone_id], countdown=review_days * 86400)
    except Exception:
        pass

    db.commit()
    return {"message": "Delivery submitted. Awaiting buyer review.", "auto_release_days": review_days}


# ══════════════════════════════════════════════════════════════════
#  BUYER: Approve delivery → release escrow to seller
# ══════════════════════════════════════════════════════════════════

@router.post("/{transaction_id}/milestones/{milestone_id}/approve")
def approve_milestone(
    transaction_id: str,
    milestone_id: str,
    body: ApproveMilestoneBody = Body(default=ApproveMilestoneBody()),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Buyer approves delivery. Escrow released instantly to seller available balance."""
    from app.models.currency import PlatformSettings

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
    if milestone.status not in ("under_review", "delivered"):
        raise HTTPException(status_code=409, detail=f"Cannot approve milestone in status '{milestone.status}'")

    ps = db.query(PlatformSettings).filter(PlatformSettings.id == "default").first()
    threshold = ps.high_value_checklist_threshold if ps else None
    if threshold is not None and float(milestone.amount) >= float(threshold):
        c = body.checklist
        if not c or not (c.files_received and c.matches_description and c.meets_requirements):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "CHECKLIST_REQUIRED",
                    "message": "This milestone exceeds the high-value threshold. Confirm all checklist items before approving.",
                },
            )

    _append_milestone_log(
        milestone,
        current_user.id,
        "approved",
        {"checklist": body.checklist.model_dump() if body.checklist else None},
    )
    _release_milestone_escrow(db, tx, milestone, body.feedback)
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
    if milestone.status not in ("under_review", "delivered"):
        raise HTTPException(status_code=409, detail="Can only request revision while delivery is under review")

    milestone.status = "revision_requested"
    milestone.revision_note = note
    milestone.auto_release_at = None
    _append_milestone_log(milestone, current_user.id, "revision_requested", {"note": note[:500]})

    if tx.seller_id:
        create_notification(
            db,
            tx.seller_id,
            "Revision Requested",
            f"Buyer requested changes on '{milestone.title}': {note[:200]}",
            "transaction",
            related_item_id=tx.id,
            related_item_type="transaction",
        )
    db.commit()
    return {"message": "Revision requested. Seller will re-submit delivery."}


# ══════════════════════════════════════════════════════════════════
#  BUYER: Report invalid / empty delivery (escrow unchanged; audit trail)
# ══════════════════════════════════════════════════════════════════


@router.post("/{transaction_id}/milestones/{milestone_id}/report-invalid-delivery")
def report_invalid_delivery(
    transaction_id: str,
    milestone_id: str,
    payload: InvalidDeliveryReport,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    note = payload.note
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can report delivery issues")

    milestone = db.query(Milestone).filter(
        Milestone.id == milestone_id, Milestone.transaction_id == transaction_id
    ).first()
    if not milestone:
        raise HTTPException(status_code=404, detail="Milestone not found")
    if milestone.status not in ("under_review", "delivered"):
        raise HTTPException(status_code=409, detail="Invalid delivery can only be reported while the delivery is under review")

    milestone.invalid_delivery_reported = True
    milestone.invalid_delivery_report_note = note.strip()
    _append_milestone_log(
        milestone,
        current_user.id,
        "invalid_delivery_reported",
        {"note": note.strip()[:500]},
    )
    if tx.seller_id:
        create_notification(
            db,
            tx.seller_id,
            "Buyer reported a delivery issue",
            f"On '{milestone.title}': {note.strip()[:200]}",
            "transaction",
            related_item_id=tx.id,
            related_item_type="transaction",
        )
    db.commit()
    return {"message": "Report recorded. You can still approve, request a revision, or open a dispute."}
