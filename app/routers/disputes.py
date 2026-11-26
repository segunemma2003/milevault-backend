from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.dispute import DisputeCreate, DisputeUpdate, DisputeDocumentCreate
from app.models.dispute import Dispute, DisputeDocument
from app.models.transaction import Transaction, Milestone
from app.models.user import User
from app.dependencies import get_current_user
from app.services.notification_service import create_notification

router = APIRouter(prefix="/disputes", tags=["disputes"])


def dispute_to_dict(d: Dispute) -> dict:
    return {
        "id": d.id,
        "transaction_id": d.transaction_id,
        "milestone_id": d.milestone_id,
        "raised_by": d.raised_by,
        "title": d.title,
        "description": d.description,
        "reason": d.reason,
        "suggested_resolution": d.suggested_resolution,
        "status": d.status,
        "resolution": d.resolution,
        "evidence_urls": d.evidence_urls or [],
        "created_at": d.created_at,
        "updated_at": d.updated_at,
        "documents": [
            {"id": doc.id, "file_url": doc.file_url, "file_name": doc.file_name, "created_at": doc.created_at}
            for doc in d.documents
        ],
    }


@router.post("", status_code=201)
def create_dispute(
    payload: DisputeCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tx = db.query(Transaction).filter(Transaction.id == payload.transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if tx.buyer_id != current_user.id and tx.seller_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    milestone = None
    if payload.milestone_id:
        milestone = db.query(Milestone).filter(
            Milestone.id == payload.milestone_id,
            Milestone.transaction_id == payload.transaction_id,
        ).first()
        if not milestone:
            raise HTTPException(status_code=404, detail="Milestone not found for this transaction")

    # Dispute fee (set by admin/finance), paid by whoever opens the dispute.
    from app.services import fees, ledger as _ledger
    disputed = float(milestone.amount) if milestone else float(tx.amount)
    fee_cur = (fees.platform_settings(db).fee_currency or "USD").upper()
    dispute_fee = fees.dispute_fee(db, disputed)

    dispute = Dispute(
        transaction_id=payload.transaction_id,
        milestone_id=payload.milestone_id,
        raised_by=current_user.id,
        title=payload.title,
        description=payload.description,
        reason=payload.reason,
        suggested_resolution=payload.suggested_resolution,
        evidence_urls=list(payload.evidence_urls),
    )
    db.add(dispute)
    db.flush()
    charged = None
    if dispute_fee > 0:
        try:
            charged = fees.collect_from_any_balance(
                db, user_id=current_user.id, amount=dispute_fee, currency=fee_cur,
                reference_type="dispute_fee", reference_id=dispute.id,
                description=f"Dispute fee for '{tx.title}'", idempotency_key=dispute.id)
        except _ledger.InsufficientFunds:
            db.rollback()
            raise HTTPException(status_code=402, detail={
                "error": "DISPUTE_FEE_UNPAID",
                "message": f"Opening a dispute costs {fee_cur} {dispute_fee:,.2f}. Add funds to your wallet and try again."})
        from app.models.wallet import WalletTransaction
        db.add(WalletTransaction(user_id=current_user.id, type="dispute_fee", amount=float(charged[1]), currency=charged[0],
                                 status="completed", transaction_id=tx.id, description=f"Dispute fee for '{tx.title}'"))

    for i, url in enumerate(payload.evidence_urls):
        u = (url or "").strip()
        if not u:
            continue
        db.add(
            DisputeDocument(
                dispute_id=dispute.id,
                file_url=u,
                file_name=f"evidence_{i + 1}",
            )
        )

    tx.status = "disputed"
    if milestone:
        milestone.status = "disputed"

    db.commit()
    db.refresh(dispute)

    other_user_id = tx.seller_id if tx.buyer_id == current_user.id else tx.buyer_id
    if other_user_id:
        create_notification(
            db,
            other_user_id,
            "Dispute Filed",
            f"A dispute has been filed for transaction '{tx.title}'.",
            "dispute",
            related_item_id=dispute.id,
            related_item_type="dispute",
        )
        db.commit()

    return dispute_to_dict(dispute)


@router.get("")
def list_disputes(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    disputes = db.query(Dispute).filter(Dispute.raised_by == current_user.id).order_by(Dispute.created_at.desc()).all()
    return [dispute_to_dict(d) for d in disputes]


@router.get("/{dispute_id}")
def get_dispute(
    dispute_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    tx = db.query(Transaction).filter(Transaction.id == dispute.transaction_id).first()
    if dispute.raised_by != current_user.id and (not tx or (tx.buyer_id != current_user.id and tx.seller_id != current_user.id)):
        raise HTTPException(status_code=403, detail="Access denied")

    return dispute_to_dict(dispute)


@router.put("/{dispute_id}")
def update_dispute(
    dispute_id: str,
    payload: DisputeUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(dispute, field, value)
    db.commit()
    db.refresh(dispute)
    return dispute_to_dict(dispute)


@router.post("/{dispute_id}/documents", status_code=201)
def upload_dispute_document(
    dispute_id: str,
    body: DisputeDocumentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Attach an additional evidence file by S3 key (after client-side presigned upload)."""
    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute or dispute.raised_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    key = body.s3_key.strip()
    if not key:
        raise HTTPException(status_code=422, detail="s3_key is required")

    doc = DisputeDocument(
        dispute_id=dispute_id,
        file_url=f"s3:{key}",
        file_name=body.filename or "evidence",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return {"id": doc.id, "file_url": doc.file_url, "file_name": doc.file_name}

