from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from app.database import get_db
from app.schemas.transaction import TransactionCreate, TransactionUpdate, TransactionOut, MilestoneCreate, MilestoneUpdate, MilestoneOut
from app.models.transaction import Transaction, Milestone
from app.models.user import User
from app.dependencies import get_current_user
from app.services.notification_service import create_notification

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
        from datetime import datetime
        milestone.completed_date = datetime.utcnow()
        tx.completed_milestones = (tx.completed_milestones or 0) + 1

    db.commit()
    db.refresh(milestone)
    return milestone
