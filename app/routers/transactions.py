from datetime import datetime, timedelta
from pydantic import BaseModel
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
from app.services import ledger
from app.services.ledger import InsufficientFunds
from app.config import settings
import logging
logger = logging.getLogger("milevault.transactions")

router = APIRouter(prefix="/transactions", tags=["transactions"])


def _send_transaction_invite_email(to_email: str, inviter_name: str, tx_title: str, tx_id: str) -> None:
    """Email an unregistered counterparty inviting them to register and accept the transaction."""
    from app.services.email_template import button, render_email, sender as email_sender
    register_url = f"{settings.FRONTEND_URL}/register"
    body_html = (
        f"<h2>You've been invited to a transaction on Bondrova</h2>"
        f"<p>Hi there,</p>"
        f"<p><strong>{inviter_name}</strong> has invited you to participate in an escrow transaction: "
        f"<strong>{tx_title}</strong>.</p>"
        f"<p>Bondrova is a secure escrow platform that protects both buyers and sellers during online transactions.</p>"
        f"<p>To accept this invitation, create your free account:</p>"
        f"<p>{button(register_url, 'Create Account &amp; Accept')}</p>"
        f"<p>Once registered with this email address, the transaction will automatically appear in your dashboard.</p>"
        f"<p>If you did not expect this invitation, you can safely ignore this email.</p>"
    )
    if not settings.RESEND_API_KEY:
        logger.info(f"[DEV INVITE EMAIL] To: {to_email} | Tx: {tx_id}")
        return
    try:
        import httpx
        resp = httpx.post(
            "https://api.resend.com/emails",
            json={"from": email_sender(), "to": [to_email], "subject": f"You've been invited to '{tx_title}' on Bondrova", "html": render_email(body_html)},
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            timeout=10,
        )
        if not resp.is_success:
            logger.error(f"Resend invite email error {resp.status_code}: {resp.text}")
    except Exception as exc:
        logger.warning(f"Invite email failed ({to_email}): {exc}")


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
        "buyer_fee": m.buyer_fee or 0,
        "seller_fee": m.seller_fee or 0,
        "fund_target": round(float(m.amount) + float(m.buyer_fee or 0), 2),
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
            "trust_score": buyer.trust_score,
            "rating": buyer.rating, "rating_count": buyer.rating_count,
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
            "trust_score": seller.trust_score,
            "rating": seller.rating, "rating_count": seller.rating_count,
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
        "counterparty_email": tx.counterparty_email,
        "last_reminded_at": tx.last_reminded_at.isoformat() if getattr(tx, "last_reminded_at", None) else None,
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
                    "message": "When you initiate as the seller, the counterparty must be a registered Bondrova user (the buyer who will fund the work).",
                },
            )
        buyer_id = counterparty.id
        seller_id = current_user.id
    else:
        buyer_id = current_user.id
        seller_id = counterparty.id if counterparty else None

    buyer_user = db.query(User).filter(User.id == buyer_id).first()
    seller_user = db.query(User).filter(User.id == seller_id).first() if seller_id else None
    if not buyer_user:
        raise HTTPException(status_code=500, detail="Buyer record missing")
    from app.services.reputation_limits import max_deal_amount_for_parties

    deal_cap = max_deal_amount_for_parties(db, buyer_user, seller_user)
    if float(payload.amount) > deal_cap:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "REPUTATION_AMOUNT_CAP",
                "message": (
                    f"This deal amount exceeds the current limit ({deal_cap:,.2f}) based on the buyer/seller "
                    "trust profile (ratings, dispute rate) and platform settings."
                ),
                "max_amount": deal_cap,
            },
        )

    # Every deal is priced in USD (the platform base). A deal entered in another currency is
    # converted once, at today's rate, and that rate is locked on the deal for the record.
    entered_cur = (payload.currency or "USD").upper()
    to_usd = 1.0
    if entered_cur != "USD":
        from app.routers.wallet import _get_exchange_rate
        to_usd = _mid_rate(db, entered_cur, "USD")
    deal_amount = round(float(payload.amount) * to_usd, 2)
    if deal_amount <= 0:
        raise HTTPException(status_code=422, detail={"error": "INVALID_AMOUNT", "message": "Deal amount must be more than zero."})
    ms_in = list(payload.milestones or [])
    if ms_in:
        ms_total = round(sum(float(m.amount) * to_usd for m in ms_in), 2)
        if abs(ms_total - deal_amount) > 0.01:
            raise HTTPException(status_code=422, detail={
                "error": "MILESTONES_DONT_ADD_UP",
                "message": f"Milestones add up to {ms_total:,.2f} but the deal is {deal_amount:,.2f} USD."})

    tx = Transaction(
        title=payload.title,
        description=payload.description,
        amount=deal_amount,
        currency="USD",
        type="milestone" if len(ms_in) > 1 else "one_time",
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
        milestones_count=max(len(ms_in), 1),
    )
    db.add(tx)
    db.flush()

    tx.locked_exchange_rate = to_usd
    tx.locked_rate_from = entered_cur
    tx.locked_rate_to = "USD"

    created_ms = []
    if not ms_in:
        # A deal without milestones is one payment, released in full when the buyer approves.
        created_ms.append(Milestone(transaction_id=tx.id, title="Full payment", description=payload.description,
                                    amount=deal_amount, currency="USD", due_date=payload.expected_completion_date))
        db.add(created_ms[-1])
    for m in ms_in:
        milestone = Milestone(
            transaction_id=tx.id,
            title=m.title,
            description=m.description,
            amount=round(float(m.amount) * to_usd, 2),
            currency="USD",
            due_date=m.due_date,
            expectations=m.expectations,
            percentage_of_total=m.percentage_of_total,
            attachments=m.attachments or [],
            supporting_documents=m.supporting_documents or [],
        )
        db.add(milestone)
        created_ms.append(milestone)

    # The escrow fee (rate and who pays) is set by admin/finance and locked on the deal now.
    from app.services import fees
    fees.allocate_deal_fees(db, tx, created_ms)

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
    elif not seller_id and raw_email and payload.initiated_as == "buyer":
        # Seller is not yet registered — send invite email in background
        import threading
        inviter_name = f"{current_user.first_name} {current_user.last_name}".strip()
        threading.Thread(
            target=_send_transaction_invite_email,
            args=(raw_email, inviter_name, tx.title, tx.id),
            daemon=True,
        ).start()

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
    if tx.status in ("funding_in_progress", "partially_funded", "active", "approved"):
        from app.services.funding_deadline_enforcement import enforce_funding_deadline_if_due

        out = enforce_funding_deadline_if_due(db, transaction_id)
        if out.get("commit"):
            db.commit()
        tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
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

    from app.services.agent_fee_service import refund_held_agent_fees_for_transaction

    refund_held_agent_fees_for_transaction(db, tx)
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
#  Initiator: Send a reminder to the counterparty
# ══════════════════════════════════════════════════════════════════

@router.post("/{transaction_id}/remind")
def remind_counterparty(
    transaction_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Initiator can nudge the counterparty. Rate-limited to once per hour per transaction.
    - Registered counterparty: creates an in-app notification
    - Unregistered counterparty: re-sends the invite email
    """
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")

    initiator_id = getattr(tx, "initiated_by_user_id", None) or tx.buyer_id
    if str(current_user.id) != str(initiator_id):
        raise HTTPException(status_code=403, detail="Only the initiator can send reminders")

    if tx.status not in ("pending_approval", "pending_acceptance"):
        raise HTTPException(status_code=409, detail="Reminders can only be sent while the invitation is open")

    # Rate limit: 1 reminder per hour
    last_reminded = getattr(tx, "last_reminded_at", None)
    if last_reminded and (datetime.utcnow() - last_reminded).total_seconds() < 3600:
        wait_minutes = int((3600 - (datetime.utcnow() - last_reminded).total_seconds()) / 60) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Please wait {wait_minutes} more minute(s) before sending another reminder.",
        )

    tx.last_reminded_at = datetime.utcnow()
    db.commit()

    inviter_name = f"{current_user.first_name} {current_user.last_name}".strip()

    if tx.seller_id:
        # Registered counterparty — in-app notification
        notify_id = tx.seller_id if str(tx.buyer_id) == str(current_user.id) else tx.buyer_id
        create_notification(
            db,
            notify_id,
            "Transaction reminder",
            f"{inviter_name} is reminding you to respond to the transaction: {tx.title}",
            "transaction",
            related_item_id=tx.id,
            related_item_type="transaction",
        )
    elif tx.counterparty_email:
        # Unregistered counterparty — resend invite email
        import threading
        threading.Thread(
            target=_send_transaction_invite_email,
            args=(tx.counterparty_email, inviter_name, tx.title, tx.id),
            daemon=True,
        ).start()
    else:
        raise HTTPException(status_code=409, detail="No counterparty linked to this transaction")

    return {"message": "Reminder sent."}


# ══════════════════════════════════════════════════════════════════
#  BUYER: Fund a milestone (moves funds from available → escrow)
# ══════════════════════════════════════════════════════════════════

FUNDABLE_STATUSES = ("funding_in_progress", "partially_funded", "active", "approved", "in_progress", "awaiting_funding")


def _mid_rate(db: Session, from_code: str, to_code: str) -> float:
    """Rate without the platform spread (used to price deals and to show amounts)."""
    from app.routers.wallet import _get_exchange_rate
    if from_code == to_code:
        return 1.0
    from app.models.currency import Currency, ExchangeRate
    f = db.query(Currency).filter(Currency.code == from_code).first()
    t = db.query(Currency).filter(Currency.code == to_code).first()
    if f and t:
        row = db.query(ExchangeRate).filter(ExchangeRate.from_currency_id == f.id, ExchangeRate.to_currency_id == t.id,
                                            ExchangeRate.is_active == True).first()  # noqa: E712
        if row:
            return float(row.rate)
        inv = db.query(ExchangeRate).filter(ExchangeRate.from_currency_id == t.id, ExchangeRate.to_currency_id == f.id,
                                            ExchangeRate.is_active == True).first()  # noqa: E712
        if inv and inv.rate:
            return 1.0 / float(inv.rate)
    return _get_exchange_rate(db, from_code, to_code)


def _cover_from_other_currency(db: Session, user: User, currency: str, need: float, pay_from: Optional[str]) -> Optional[dict]:
    """
    If the buyer chose to pay from another balance (e.g. their naira), convert just the shortfall
    into the deal currency first, at the platform rate (spread included). Returns what was
    converted, or None if nothing was needed.
    """
    from decimal import ROUND_UP
    from app.routers.wallet import _get_exchange_rate
    src = (pay_from or "").upper()
    if not src or src == currency:
        return None
    have = ledger.to_decimal(ledger.lock_balance(db, user_id=user.id, currency=currency).amount or 0)
    shortfall = ledger.to_decimal(need) - have
    if shortfall <= 0:
        return None
    # Priced as users think of it: 1 USD costs `rate` NGN (platform spread included).
    rate = ledger.to_decimal(_get_exchange_rate(db, currency, src))
    src_amount = (shortfall * rate).quantize(ledger.to_decimal("0.01"), rounding=ROUND_UP)
    try:
        ledger.debit(db, user_id=user.id, currency=src, amount=src_amount, from_bucket="available",
                     reference_type="fx_convert", description=f"Convert {src} → {currency} (1 {currency} = {rate} {src}) to fund a deal")
    except InsufficientFunds as e:
        db.rollback()
        raise HTTPException(status_code=400, detail={
            "error": "INSUFFICIENT_FUNDS",
            "message": f"Not enough {src}: you have {float(e.have):,.2f}, need {float(src_amount):,.2f} ({currency} {float(shortfall):,.2f})."})
    ledger.credit(db, user_id=user.id, currency=currency, amount=shortfall, to_bucket="available",
                  reference_type="fx_convert", description=f"Convert {src} → {currency} (1 {currency} = {rate} {src}) to fund a deal")
    db.add(WalletTransaction(user_id=user.id, type="conversion", amount=float(src_amount), currency=src, status="completed",
                             description=f"Converted {src} {float(src_amount):,.2f} → {currency} {float(shortfall):,.2f} (1 {currency} = {rate} {src}) to fund a deal"))
    return {"from": src, "amount": float(src_amount), "to": currency, "converted": float(shortfall), "rate": float(rate)}


def _fund_milestone_core(db: Session, tx: Transaction, milestone: Milestone, user: User, amount: Optional[float]) -> tuple:
    """Move up to the milestone's remaining amount from the buyer's available balance into escrow.
    Returns (paid, became_fully_funded). The milestone row must already be locked."""
    from app.services.fees import fund_target
    currency = milestone.currency or tx.currency
    target = fund_target(milestone)
    already = float(milestone.funded_amount or 0)
    remaining = round(target - already, 8)
    if milestone.is_funded or remaining <= 0:
        return 0.0, False
    pay = round(min(float(amount) if amount is not None else remaining, remaining), 8)
    if pay <= 0:
        return 0.0, False
    pay = float(ledger.quantize(pay, currency, db))
    try:
        ledger.move(
            db, user_id=user.id, currency=currency, amount=pay,
            from_bucket="available", to_bucket="escrow",
            reference_type="milestone_fund", reference_id=milestone.id,
            description=f"Escrow lock for milestone '{milestone.title}'",
        )
    except InsufficientFunds as e:
        db.rollback()
        raise HTTPException(status_code=400, detail={
            "error": "INSUFFICIENT_FUNDS",
            "message": f"Insufficient balance. You have {currency} {float(e.have):.2f}, need {currency} {pay:.2f}. Deposit funds, or pay from another balance.",
        })
    new_funded = round(already + pay, 8)
    milestone.funded_amount = new_funded
    became = new_funded >= target - 1e-9
    if became:
        milestone.is_funded = True
        milestone.status = "funded"
        milestone.funded_amount = target
        tx.funded_milestones = (tx.funded_milestones or 0) + 1
    else:
        milestone.is_funded = False
        milestone.status = "pending"
    db.add(WalletTransaction(
        user_id=user.id, type="escrow_lock", amount=pay, currency=currency, status="completed",
        transaction_id=tx.id, milestone_id=milestone.id, description=f"Escrow for milestone: {milestone.title}",
    ))
    return pay, became


def _ordered_milestones(db: Session, tx_id: str) -> list:
    return db.query(Milestone).filter(Milestone.transaction_id == tx_id).order_by(
        Milestone.created_at.asc(), Milestone.id.asc()).all()


def _after_funding(db: Session, tx: Transaction, newly_full: list, paid_total: float, currency: str) -> None:
    """Deal status + notifications after money went into escrow."""
    was_paused = tx.status == "awaiting_funding"
    ms = _ordered_milestones(db, tx.id)
    if any(m.is_funded and m.status != "completed" for m in ms):
        if tx.status in ("funding_in_progress", "partially_funded", "approved", "awaiting_funding"):
            tx.status = "active"
    elif any((m.funded_amount or 0) > 0 and not m.is_funded for m in ms) and tx.status in ("funding_in_progress", "approved"):
        tx.status = "partially_funded"
    if not tx.seller_id:
        return
    if newly_full:
        names = ", ".join(f"'{m.title}'" for m in newly_full)
        title = "Deal resumed — milestone funded" if was_paused else "Milestone funded — you can start"
        create_notification(
            db, tx.seller_id, title,
            f"{names} in '{tx.title}' is fully paid ({currency} {paid_total:,.2f} added to escrow). You can start work.",
            "transaction", related_item_id=tx.id, related_item_type="transaction")
    else:
        create_notification(
            db, tx.seller_id, "Partial payment received",
            f"{currency} {paid_total:,.2f} added to escrow for '{tx.title}'. Work can start once a milestone is 100% paid.",
            "transaction", related_item_id=tx.id, related_item_type="transaction")


def _buyer_fund_guard(db: Session, transaction_id: str, user: User) -> Transaction:
    from app.services.funding_deadline_enforcement import enforce_funding_deadline_if_due
    fd_out = enforce_funding_deadline_if_due(db, transaction_id)
    if fd_out.get("commit"):
        db.commit()
    if user.wallet_frozen:
        raise HTTPException(status_code=403, detail={"error": "WALLET_FROZEN", "message": "Your wallet has been frozen. Contact support."})
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).with_for_update().first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can fund milestones")
    if tx.status not in FUNDABLE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Cannot fund milestones in transaction status '{tx.status}'")
    return tx


@router.post("/{transaction_id}/milestones/{milestone_id}/fund")
def fund_milestone(
    transaction_id: str,
    milestone_id: str,
    body: FundMilestoneBody = Body(default=FundMilestoneBody()),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Buyer funds a milestone (full or partial). Work on a milestone can only start once it is
    100% funded. Optional pay_from (e.g. "NGN") converts the shortfall from that balance first.
    """
    tx = _buyer_fund_guard(db, transaction_id, current_user)
    # Lock the milestone row: concurrent fund requests for the same milestone serialise here,
    # so each one sees the other's funded_amount (no lost update / double charge).
    milestone = db.query(Milestone).filter(
        Milestone.id == milestone_id, Milestone.transaction_id == transaction_id
    ).with_for_update().first()
    if not milestone:
        raise HTTPException(status_code=404, detail="Milestone not found")
    from app.services.fees import fund_target
    if milestone.is_funded or fund_target(milestone) - float(milestone.funded_amount or 0) <= 0:
        raise HTTPException(status_code=409, detail="Milestone is already fully funded")
    if body.amount is not None and body.amount <= 0:
        raise HTTPException(status_code=422, detail={"error": "INVALID_AMOUNT", "message": "Funding amount must be greater than zero."})

    currency = milestone.currency or tx.currency
    remaining = round(fund_target(milestone) - float(milestone.funded_amount or 0), 8)
    want = min(float(body.amount), remaining) if body.amount is not None else remaining
    converted = _cover_from_other_currency(db, current_user, currency, want, body.pay_from)
    pay, became = _fund_milestone_core(db, tx, milestone, current_user, want)
    _after_funding(db, tx, [milestone] if became else [], pay, currency)
    db.commit()
    return {
        "message": f"Milestone '{milestone.title}' funded." + (" Work may begin." if became else " Partial funding recorded."),
        "funded_amount": milestone.funded_amount,
        "is_funded": milestone.is_funded,
        "currency": currency,
        "converted": converted,
        "deal_status": tx.status,
    }


@router.post("/{transaction_id}/fund")
def fund_deal(
    transaction_id: str,
    body: FundMilestoneBody = Body(default=FundMilestoneBody()),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Pay the whole deal at once (omit amount), or any amount: it fills milestones in order,
    first to last. Money still releases milestone by milestone. A milestone only becomes
    workable once it is 100% paid — an amount that covers no milestone fully starts nothing.
    """
    tx = _buyer_fund_guard(db, transaction_id, current_user)
    ms = [m for m in db.query(Milestone).filter(Milestone.transaction_id == tx.id)
          .order_by(Milestone.created_at.asc(), Milestone.id.asc()).with_for_update().all()
          if not m.is_funded and m.status != "completed"]
    from app.services.fees import fund_target
    outstanding = round(sum(fund_target(m) - float(m.funded_amount or 0) for m in ms), 8)
    if outstanding <= 0:
        raise HTTPException(status_code=409, detail="This deal is already fully paid.")
    if body.amount is not None and body.amount <= 0:
        raise HTTPException(status_code=422, detail={"error": "INVALID_AMOUNT", "message": "Funding amount must be greater than zero."})
    budget = min(float(body.amount), outstanding) if body.amount is not None else outstanding
    currency = tx.currency or "USD"
    converted = _cover_from_other_currency(db, current_user, currency, budget, body.pay_from)

    paid_total, newly_full, left = 0.0, [], budget
    for m in ms:
        if left <= 1e-9:
            break
        pay, became = _fund_milestone_core(db, tx, m, current_user, left)
        paid_total = round(paid_total + pay, 8)
        left = round(left - pay, 8)
        if became:
            newly_full.append(m)
    _after_funding(db, tx, newly_full, paid_total, currency)
    db.commit()
    return {
        "message": (f"{currency} {paid_total:,.2f} paid into escrow. "
                    + (f"{len(newly_full)} milestone(s) fully funded — work can start." if newly_full
                       else "No milestone is fully paid yet, so work can't start until one is.")),
        "paid": paid_total, "fully_funded": [m.id for m in newly_full], "outstanding": round(outstanding - paid_total, 2),
        "currency": currency, "converted": converted, "deal_status": tx.status,
    }


@router.post("/{transaction_id}/request-start")
def request_start(
    transaction_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Buyer nudges the seller to start a milestone that is fully paid but not started yet."""
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the buyer can ask the seller to start")
    ready = [m for m in _ordered_milestones(db, tx.id) if m.is_funded and m.status == "funded"]
    if not ready or not tx.seller_id:
        raise HTTPException(status_code=409, detail={"error": "NOTHING_TO_START",
                            "message": "There's no fully paid milestone waiting to start. Pay a milestone in full first."})
    now = datetime.utcnow()
    if tx.last_reminded_at and now - tx.last_reminded_at < timedelta(hours=1):
        raise HTTPException(status_code=429, detail={"error": "TOO_SOON", "message": "You already asked within the last hour."})
    tx.last_reminded_at = now
    create_notification(
        db, tx.seller_id, "Buyer asked you to start",
        f"{current_user.first_name} has paid for '{ready[0].title}' in '{tx.title}' and asked you to start work.",
        "transaction", related_item_id=tx.id, related_item_type="transaction")
    db.commit()
    return {"message": "We've let the seller know you'd like them to start."}


# ══════════════════════════════════════════════════════════════════
#  SELLER: Start work on a funded milestone (funded → in_progress)
# ══════════════════════════════════════════════════════════════════

@router.post("/{transaction_id}/milestones/{milestone_id}/start")
def start_milestone(
    transaction_id: str,
    milestone_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Seller explicitly marks a funded milestone as in progress."""
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the seller can start work on a milestone")

    milestone = db.query(Milestone).filter(
        Milestone.id == milestone_id, Milestone.transaction_id == transaction_id
    ).first()
    if not milestone:
        raise HTTPException(status_code=404, detail="Milestone not found")
    if not milestone.is_funded:
        raise HTTPException(status_code=409, detail="Cannot start an unfunded milestone")
    if milestone.status != "funded":
        raise HTTPException(status_code=409, detail=f"Milestone is already in status '{milestone.status}'")

    milestone.status = "in_progress"
    _append_milestone_log(milestone, current_user.id, "work_started", {})

    create_notification(
        db, tx.buyer_id,
        "Work Started",
        f"Seller has started work on '{milestone.title}'.",
        "transaction",
        related_item_id=tx.id,
        related_item_type="transaction",
    )
    db.commit()
    return {"message": f"Milestone '{milestone.title}' is now in progress."}


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
    try:
        _release_milestone_escrow(db, tx, milestone, body.feedback)
    except InsufficientFunds:
        db.rollback()
        logger.error("Escrow shortfall releasing milestone %s of tx %s", milestone_id, transaction_id)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "ESCROW_MISMATCH",
                "message": "The escrow balance for this milestone doesn't match. Our team has been alerted — please contact support.",
            },
        )
    db.commit()
    return {"message": "Milestone approved. Funds released to seller.", "amount": milestone.amount}


def _release_milestone_escrow(db: Session, tx: Transaction, milestone: Milestone, feedback: str = None):
    """Internal helper: release milestone escrow to seller. Called by approve and auto-release."""
    # Re-read the milestone under a row lock so two releases (approve + auto-release, or a
    # double click) can't both run; the one-time key below makes any repeat a no-op.
    milestone = db.query(Milestone).filter(Milestone.id == milestone.id).with_for_update().one()
    if milestone.status == "completed":
        return False

    currency = milestone.currency or tx.currency
    escrowed = float(milestone.funded_amount or milestone.amount)
    fee_total = round(float(milestone.buyer_fee or 0) + float(milestone.seller_fee or 0), 8)
    fee_total = min(fee_total, escrowed)
    amount = round(escrowed - fee_total, 8)   # what the seller receives

    # Buyer escrow → seller available. Raises InsufficientFunds if the buyer's escrow can't
    # cover it (previously this silently clamped escrow to 0 and paid the seller anyway).
    released = ledger.transfer(
        db, currency=currency, amount=amount,
        from_user_id=tx.buyer_id, from_bucket="escrow",
        to_user_id=tx.seller_id, to_bucket="available",
        reference_type="milestone_release", reference_id=milestone.id,
        description=f"Release for milestone '{milestone.title}'",
        idempotency_key=milestone.id,
    )
    if not released:
        return False
    if fee_total > 0:
        from app.services import fees
        fees.collect(db, user_id=tx.buyer_id, currency=currency, amount=fee_total, from_bucket="escrow",
                     reference_type="escrow_fee", reference_id=milestone.id,
                     description=f"Bondrova fee for milestone '{milestone.title}'", idempotency_key=milestone.id)

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

    # Check if all milestones complete
    all_milestones = _ordered_milestones(db, tx.id)
    if all(m.status == "completed" for m in all_milestones):
        tx.status = "completed"
        # Update total volume
        seller = db.query(User).filter(User.id == tx.seller_id).first()
        if seller:
            seller.total_volume = round((seller.total_volume or 0) + amount, 2)
        from app.services.agent_fee_service import settle_held_agent_fees_on_transaction_completed

        settle_held_agent_fees_on_transaction_completed(db, tx)

    # Trust Scores for both sides (after the deal's status is final, so this deal counts)
    db.flush()
    _update_reputation(db, tx.seller_id)
    _update_reputation(db, tx.buyer_id)

    create_notification(
        db, tx.seller_id,
        "Payment Released!",
        f"Your payment of {currency} {amount:.2f} for '{milestone.title}' has been released to your wallet"
        + (f" (Bondrova fee {currency} {float(milestone.seller_fee or 0):.2f})." if (milestone.seller_fee or 0) > 0 else "."),
        "transaction", related_item_id=tx.id, related_item_type="transaction",
    )
    create_notification(
        db, tx.buyer_id,
        "Milestone completed",
        f"'{milestone.title}' is complete and {currency} {amount:.2f} was released to the seller.",
        "transaction", related_item_id=tx.id, related_item_type="transaction",
    )

    # Next milestone not fully paid and nothing else paid in escrow → pause the deal until it is.
    if tx.status != "completed":
        open_ms = [m for m in all_milestones if m.status != "completed"]
        ongoing = {"funded", "in_progress", "delivered", "under_review", "revision_requested", "disputed"}
        # Only pause when nothing is paid or in progress: an ongoing milestone is never paused.
        if open_ms and not any(m.is_funded or m.status in ongoing for m in open_ms):
            nxt = open_ms[0]
            due = round(float(nxt.amount) - float(nxt.funded_amount or 0), 2)
            tx.status = "awaiting_funding"
            create_notification(
                db, tx.buyer_id, "Deal paused — fund the next milestone",
                f"'{tx.title}' is paused until '{nxt.title}' is fully paid ({currency} {due:,.2f} to go).",
                "transaction", related_item_id=tx.id, related_item_type="transaction",
            )
            if tx.seller_id:
                create_notification(
                    db, tx.seller_id, "Deal paused",
                    f"'{tx.title}' is paused until the buyer pays for '{nxt.title}'. We'll tell you when it's funded.",
                    "transaction", related_item_id=tx.id, related_item_type="transaction",
                )
    return True


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
    db.flush()
    from app.services import trust_score
    trust_score.recompute(db, user, "Milestone completed")


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



# ══════════════════════════════════════════════════════════════════
#  REVIEWS — each party rates the other once a deal is completed
# ══════════════════════════════════════════════════════════════════

class ReviewIn(BaseModel):
    rating: int
    comment: Optional[str] = None


@router.post("/{transaction_id}/review", status_code=201)
def leave_review(transaction_id: str, body: ReviewIn, current_user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    from app.models.trust import DealReview
    from app.services import trust_score
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if current_user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(status_code=403, detail="Only the buyer or seller can review this deal")
    if tx.status != "completed":
        raise HTTPException(status_code=409, detail={"error": "NOT_COMPLETED", "message": "You can rate the other party once the deal is completed."})
    if not 1 <= int(body.rating) <= 5:
        raise HTTPException(status_code=422, detail={"error": "INVALID_RATING", "message": "Rating must be 1 to 5 stars."})
    if db.query(DealReview).filter_by(transaction_id=tx.id, reviewer_id=current_user.id).first():
        raise HTTPException(status_code=409, detail={"error": "ALREADY_REVIEWED", "message": "You've already rated this deal."})
    reviewee_id = tx.seller_id if current_user.id == tx.buyer_id else tx.buyer_id
    role = "seller" if current_user.id == tx.buyer_id else "buyer"
    db.add(DealReview(transaction_id=tx.id, reviewer_id=current_user.id, reviewee_id=reviewee_id, reviewee_role=role,
                      rating=int(body.rating), comment=(body.comment or "").strip()[:1000] or None))
    db.flush()
    reviewee = db.query(User).filter(User.id == reviewee_id).first()
    trust_score.recompute(db, reviewee, f"New {body.rating}-star review")
    create_notification(db, reviewee_id, "You got a new review",
                        f"{current_user.first_name} rated you {body.rating}/5 for '{tx.title}'.", "transaction",
                        related_item_id=tx.id, related_item_type="transaction")
    db.commit()
    return {"message": "Thanks — your rating has been recorded."}


@router.get("/{transaction_id}/reviews")
def deal_reviews(transaction_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.models.trust import DealReview
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx or current_user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(status_code=404, detail="Transaction not found")
    rows = db.query(DealReview).filter_by(transaction_id=tx.id).all()
    return {"can_review": tx.status == "completed" and not any(r.reviewer_id == current_user.id for r in rows),
            "reviews": [{"reviewer_id": r.reviewer_id, "reviewee_role": r.reviewee_role, "rating": r.rating,
                         "comment": r.comment, "created_at": r.created_at} for r in rows]}
