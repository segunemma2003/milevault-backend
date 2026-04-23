import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from typing import List, Optional
from app.database import get_db
from app.schemas.dispute import DisputeCreate, DisputeUpdate
from app.models.dispute import Dispute, DisputeDocument
from app.models.transaction import Transaction
from app.models.user import User
from app.dependencies import get_current_user
from app.services.notification_service import create_notification
from app.config import settings

router = APIRouter(prefix="/disputes", tags=["disputes"])


def dispute_to_dict(d: Dispute) -> dict:
    return {
        "id": d.id,
        "transaction_id": d.transaction_id,
        "raised_by": d.raised_by,
        "title": d.title,
        "description": d.description,
        "reason": d.reason,
        "suggested_resolution": d.suggested_resolution,
        "status": d.status,
        "resolution": d.resolution,
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

    dispute = Dispute(
        transaction_id=payload.transaction_id,
        raised_by=current_user.id,
        title=payload.title,
        description=payload.description,
        reason=payload.reason,
        suggested_resolution=payload.suggested_resolution,
    )
    db.add(dispute)
    tx.status = "disputed"
    db.commit()
    db.refresh(dispute)

    other_user_id = tx.seller_id if tx.buyer_id == current_user.id else tx.buyer_id
    if other_user_id:
        create_notification(
            db, other_user_id,
            "Dispute Filed",
            f"A dispute has been filed for transaction '{tx.title}'.",
            "dispute",
            related_item_id=dispute.id,
            related_item_type="dispute",
        )

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
async def upload_dispute_document(
    dispute_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute or dispute.raised_by != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    if file.size and file.size > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 5MB)")

    ext = file.filename.split(".")[-1] if "." in file.filename else "bin"
    filename = f"dispute_{dispute_id}_{uuid.uuid4().hex}.{ext}"
    upload_path = os.path.join(settings.UPLOAD_DIR, "disputes")
    os.makedirs(upload_path, exist_ok=True)

    file_path = os.path.join(upload_path, filename)
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    doc = DisputeDocument(
        dispute_id=dispute_id,
        file_url=f"/uploads/disputes/{filename}",
        file_name=file.filename,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return {"id": doc.id, "file_url": doc.file_url, "file_name": doc.file_name}
