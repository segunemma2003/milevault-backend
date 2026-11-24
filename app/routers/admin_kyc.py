"""Admin KYC review: list submissions, view documents, approve/reject, revoke."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_admin
from app.models.kyc import KycDocument
from app.models.notification import Notification
from app.models.user import User
from app.routers.kyc import ADDRESS_DOC, REQUIRED_DOCS
from app.services.s3_service import get_presigned_view_url

router = APIRouter(prefix="/admin/kyc", tags=["Admin — KYC"])


def _notify(db: Session, user_id: str, title: str, message: str) -> None:
    db.add(Notification(user_id=str(user_id), title=title, message=message, type="kyc"))


def _view_url(file_url: str) -> Optional[str]:
    """Documents are stored as "s3:<key>". Older local-disk uploads were lost on redeploy."""
    if file_url and file_url.startswith("s3:"):
        try:
            return get_presigned_view_url(file_url[3:], expires=900)
        except Exception:
            return None
    return None


def _overall(docs: list) -> str:
    types_verified = {d.type for d in docs if d.status == "verified"}
    if any(d.status == "rejected" for d in docs):
        return "rejected"
    if types_verified & REQUIRED_DOCS and ADDRESS_DOC in types_verified:
        return "verified"
    if any(d.status == "pending" for d in docs):
        return "pending"
    return "incomplete"


def _recompute(db: Session, user: User) -> str:
    """Mark the user verified once one identity document AND proof of address are approved."""
    docs = db.query(KycDocument).filter(KycDocument.user_id == user.id).all()
    types_verified = {d.type for d in docs if d.status == "verified"}
    should_verify = bool(types_verified & REQUIRED_DOCS) and ADDRESS_DOC in types_verified
    if should_verify and not user.is_kyc_verified:
        user.is_kyc_verified = True
        _notify(db, user.id, "Identity Verified",
                "Your KYC verification is complete. You now have full access, including withdrawals.")
    return _overall(docs)


@router.get("", summary="KYC submissions grouped by user")
def list_submissions(
    status: str = "pending",  # pending | verified | rejected | all
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    docs = db.query(KycDocument).order_by(KycDocument.created_at.desc()).all()
    by_user: dict = {}
    for d in docs:
        by_user.setdefault(d.user_id, []).append(d)

    users = {u.id: u for u in db.query(User).filter(User.id.in_(list(by_user.keys()))).all()} if by_user else {}
    out = []
    for uid, udocs in by_user.items():
        u = users.get(uid)
        if not u:
            continue
        overall = "verified" if u.is_kyc_verified else _overall(udocs)
        if status != "all" and overall != status:
            continue
        out.append({
            "user": {
                "id": u.id,
                "name": f"{u.first_name} {u.last_name}".strip(),
                "email": u.email,
                "country_code": getattr(u, "country_code", None),
                "is_kyc_verified": u.is_kyc_verified,
            },
            "overall_status": overall,
            "submitted_at": max(d.created_at for d in udocs if d.created_at) if udocs else None,
            "documents": [
                {
                    "id": d.id,
                    "type": d.type,
                    "file_name": d.file_name,
                    "status": d.status,
                    "rejection_reason": d.rejection_reason,
                    "created_at": d.created_at,
                    "reviewed_at": d.reviewed_at,
                    "view_url": _view_url(d.file_url),
                }
                for d in udocs
            ],
        })
    out.sort(key=lambda r: r["submitted_at"] or datetime.min, reverse=True)
    return out


def _get_doc(db: Session, doc_id: str) -> KycDocument:
    doc = db.query(KycDocument).filter(KycDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Document not found."})
    return doc


@router.put("/documents/{doc_id}/approve", summary="Approve a KYC document")
def approve_document(doc_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    doc = _get_doc(db, doc_id)
    doc.status = "verified"
    doc.rejection_reason = None
    doc.reviewed_at = datetime.utcnow()
    user = db.query(User).filter(User.id == doc.user_id).first()
    overall = _recompute(db, user) if user else "pending"
    db.commit()
    return {"message": "Document approved.", "overall_status": overall, "is_kyc_verified": bool(user and user.is_kyc_verified)}


@router.put("/documents/{doc_id}/reject", summary="Reject a KYC document with a reason")
def reject_document(
    doc_id: str,
    reason: str = Body(..., embed=True, min_length=3, max_length=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    doc = _get_doc(db, doc_id)
    doc.status = "rejected"
    doc.rejection_reason = reason.strip()
    doc.reviewed_at = datetime.utcnow()
    user = db.query(User).filter(User.id == doc.user_id).first()
    if user:
        user.is_kyc_verified = False
        _notify(db, user.id, "KYC Document Rejected",
                f"Your {doc.type.replace('_', ' ')} was rejected: {doc.rejection_reason}. Please upload a new one.")
    db.commit()
    return {"message": "Document rejected."}


@router.put("/users/{user_id}/revoke", summary="Revoke a user's KYC verification")
def revoke_verification(
    user_id: str,
    reason: str = Body(..., embed=True, min_length=3, max_length=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "User not found."})
    user.is_kyc_verified = False
    _notify(db, user.id, "KYC Verification Revoked", f"Your verification was revoked: {reason.strip()}")
    db.commit()
    return {"message": "Verification revoked."}
