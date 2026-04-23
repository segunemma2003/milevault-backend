import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.kyc import KycDocument
from app.models.user import User
from app.dependencies import get_current_user
from app.services.notification_service import create_notification
from app.config import settings

router = APIRouter(prefix="/kyc", tags=["kyc"])

REQUIRED_DOCS = {"id_card", "passport", "drivers_license"}
ADDRESS_DOC = "address_proof"
SELFIE_DOC = "selfie"


@router.get("/status")
def get_kyc_status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    docs = db.query(KycDocument).filter(KycDocument.user_id == current_user.id).all()

    doc_types_submitted = {d.type for d in docs}
    doc_list = [
        {
            "id": d.id,
            "user_id": d.user_id,
            "type": d.type,
            "file_url": d.file_url,
            "file_name": d.file_name,
            "status": d.status,
            "rejection_reason": d.rejection_reason,
            "created_at": d.created_at,
        }
        for d in docs
    ]

    has_identity = bool(doc_types_submitted & REQUIRED_DOCS)
    has_address = ADDRESS_DOC in doc_types_submitted

    if not has_identity and not has_address:
        overall_status = "incomplete"
    elif all(d.status == "verified" for d in docs) and has_identity and has_address:
        overall_status = "verified"
    elif any(d.status == "rejected" for d in docs):
        overall_status = "rejected"
    elif docs:
        overall_status = "pending"
    else:
        overall_status = "incomplete"

    return {
        "is_kyc_verified": current_user.is_kyc_verified,
        "documents": doc_list,
        "overall_status": overall_status,
    }


@router.post("/upload", status_code=201)
async def upload_kyc_document(
    doc_type: str = Form(...),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    valid_types = {"id_card", "passport", "drivers_license", "address_proof", "selfie"}
    if doc_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid document type. Must be one of: {', '.join(valid_types)}")

    allowed_content_types = {"image/jpeg", "image/png", "application/pdf"}
    if file.content_type not in allowed_content_types:
        raise HTTPException(status_code=400, detail="Only JPG, PNG, and PDF files are allowed")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")

    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"kyc_{current_user.id}_{doc_type}_{uuid.uuid4().hex}.{ext}"
    upload_path = os.path.join(settings.UPLOAD_DIR, "kyc")
    os.makedirs(upload_path, exist_ok=True)

    file_path = os.path.join(upload_path, filename)
    with open(file_path, "wb") as f:
        f.write(content)

    # Remove existing doc of same type if any
    existing = db.query(KycDocument).filter(
        KycDocument.user_id == current_user.id, KycDocument.type == doc_type
    ).first()
    if existing:
        db.delete(existing)
        db.flush()

    doc = KycDocument(
        user_id=current_user.id,
        type=doc_type,
        file_url=f"/uploads/kyc/{filename}",
        file_name=file.filename,
        status="pending",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    create_notification(
        db, current_user.id,
        "KYC Document Submitted",
        f"Your {doc_type.replace('_', ' ')} has been submitted for review.",
        "kyc",
        related_item_id=doc.id,
        related_item_type="kyc",
    )

    return {
        "id": doc.id,
        "user_id": doc.user_id,
        "type": doc.type,
        "file_url": doc.file_url,
        "file_name": doc.file_name,
        "status": doc.status,
        "created_at": doc.created_at,
    }
