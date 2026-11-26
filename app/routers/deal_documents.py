"""
Deal contract (generate · sign · view), branded payment receipt (view · email), and the optional
handover code (OTP) that lets the seller confirm receipt on the buyer's behalf at handover.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_user
from app.models.transaction import Milestone, Transaction
from app.models.user import User
from app.services import deal_documents as dd
from app.services.ledger import InsufficientFunds
from app.services.notification_service import create_notification

logger = logging.getLogger("milevault.deal_documents")
router = APIRouter(prefix="/transactions", tags=["deal documents"])

HANDOVER_TTL = timedelta(days=14)
HANDOVER_MAX_ATTEMPTS = 5
HANDOVER_STATUSES = ("funded", "in_progress", "delivered", "under_review", "revision_requested")


def _deal(db: Session, transaction_id: str, user: User, lock: bool = False) -> Transaction:
    q = db.query(Transaction).filter(Transaction.id == transaction_id)
    tx = (q.with_for_update() if lock else q).first()
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if user.id not in (tx.buyer_id, tx.seller_id) and not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Access denied")
    return tx


def _doc_error(e: dd.DocumentError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail={"error": e.error, "message": e.message})


def _client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


# ─── Contract ────────────────────────────────────────────────────────────────

class SignBody(BaseModel):
    full_name: str


@router.get("/{transaction_id}/contract", summary="The deal's contract (generated from its current terms)")
def get_contract(transaction_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = _deal(db, transaction_id, current_user, lock=True)  # one new version even if opened twice at once
    c = dd.current_contract(db, tx)
    db.commit()
    return {"contract": dd.contract_to_dict(c), "contract_signed": bool(tx.contract_signed)}


@router.post("/{transaction_id}/contract/sign", summary="Sign the contract by typing your full name")
def sign_contract(transaction_id: str, body: SignBody, request: Request,
                  current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = _deal(db, transaction_id, current_user, lock=True)
    try:
        c = dd.sign(db, tx, current_user, body.full_name, _client_ip(request))
    except dd.DocumentError as e:
        raise _doc_error(e)
    other = tx.seller_id if current_user.id == tx.buyer_id else tx.buyer_id
    if c.status == "signed":
        for uid in (tx.buyer_id, tx.seller_id):
            create_notification(db, uid, "Contract signed", f"Both parties signed the agreement for '{tx.title}'.",
                                "transaction", related_item_id=tx.id, related_item_type="transaction")
    elif other:
        create_notification(db, other, "Please sign the contract",
                            f"{current_user.first_name} signed the agreement for '{tx.title}'. Review and sign it too.",
                            "transaction", related_item_id=tx.id, related_item_type="transaction")
    db.commit()
    return {"message": "Contract signed." if c.status == "signed" else "Signed. Waiting for the other party.",
            "contract": dd.contract_to_dict(c), "contract_signed": bool(tx.contract_signed)}


@router.get("/{transaction_id}/contract/document", response_class=HTMLResponse, include_in_schema=False)
def contract_document(transaction_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = _deal(db, transaction_id, current_user, lock=True)  # one new version even if opened twice at once
    c = dd.current_contract(db, tx)
    if not c:
        db.commit()
        raise HTTPException(status_code=409, detail="The contract is available once both parties are on the deal.")
    page = dd.render_contract(db, c)
    db.commit()
    return HTMLResponse(page)


# ─── Receipt ─────────────────────────────────────────────────────────────────

class ReceiptEmailBody(BaseModel):
    template: Optional[str] = None


@router.get("/{transaction_id}/documents", summary="The deal's documents: contract status and current receipt figures")
def list_documents(transaction_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = _deal(db, transaction_id, current_user)
    c = dd.current_contract(db, tx)
    contract_code = dd.issue_contract(db, c).code if c else None
    receipt, summary = dd.issue_receipt(db, tx)
    db.commit()
    return {
        "contract": dd.contract_to_dict(c), "contract_code": contract_code, "contract_signed": bool(tx.contract_signed),
        "receipt": {"code": receipt.code, "issued_at": receipt.issued_at.isoformat() + "Z", **summary},
        "verify_base": dd.verify_url(""),
        "receipt_template": getattr(tx.buyer, "receipt_template", None) or "classic",
    }


@router.get("/{transaction_id}/receipt/document", response_class=HTMLResponse, include_in_schema=False)
def receipt_document(transaction_id: str, template: Optional[str] = Query(None), currency: Optional[str] = Query(None),
                     current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = _deal(db, transaction_id, current_user)
    page = dd.render_receipt(db, tx, template, currency or dd.display_currency_for(db, current_user))
    db.commit()  # keeps the verification code printed on it
    return HTMLResponse(page)


@router.post("/{transaction_id}/receipt/email", summary="Email the deal's payment receipt to me")
def email_receipt(transaction_id: str, body: ReceiptEmailBody = Body(default_factory=ReceiptEmailBody),
                  current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = _deal(db, transaction_id, current_user)
    if not current_user.email:
        raise HTTPException(status_code=409, detail="Your account has no email address.")
    page = dd.receipt_email_html(db, tx, body.template, dd.display_currency_for(db, current_user))
    code = dd.issue_receipt(db, tx)[0].code
    db.commit()
    to = current_user.email
    subject = f"Receipt — {tx.title} ({code})"

    def _send():
        try:
            from app.routers.auth import _send_email_direct
            _send_email_direct(to, subject, page)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Receipt email to %s failed: %s", to, exc)

    from app.services.notification_dispatch import _pool
    _pool.submit(_send)
    return {"message": f"Receipt sent to {to}."}


# ─── Handover code (OTP) ─────────────────────────────────────────────────────

class HandoverConfirmBody(BaseModel):
    code: str


CLOSED_DEAL = ("cancelled", "refunded", "completed", "declined", "expired")


def _open_dispute(db: Session, tx: Transaction, m: Milestone) -> bool:
    from app.models.dispute import Dispute
    return db.query(Dispute.id).filter(
        Dispute.transaction_id == tx.id, Dispute.status.in_(("open", "in_review")),
        (Dispute.milestone_id == m.id) | (Dispute.milestone_id.is_(None))).first() is not None


def _code_hash(milestone_id: str, code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"{milestone_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _milestone(db: Session, tx: Transaction, milestone_id: str) -> Milestone:
    m = (db.query(Milestone).filter(Milestone.id == milestone_id, Milestone.transaction_id == tx.id)
         .with_for_update().first())
    if not m:
        raise HTTPException(status_code=404, detail="Milestone not found")
    return m


@router.post("/{transaction_id}/milestones/{milestone_id}/handover-code",
             summary="Buyer: get a one-time handover code to give the seller once the goods are received")
def create_handover_code(transaction_id: str, milestone_id: str,
                         current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = _deal(db, transaction_id, current_user, lock=True)
    if current_user.id != tx.buyer_id:
        raise HTTPException(status_code=403, detail="Only the buyer can create a handover code")
    m = _milestone(db, tx, milestone_id)
    if m.status not in HANDOVER_STATUSES or not m.is_funded or tx.status in CLOSED_DEAL:
        raise HTTPException(status_code=409, detail={"error": "NOT_READY",
                            "message": "A handover code is available once the milestone is fully paid and not yet released."})
    if _open_dispute(db, tx, m):
        raise HTTPException(status_code=409, detail={"error": "DISPUTED",
                            "message": "There's an open dispute on this milestone, so it can't be released with a code."})
    code = f"{secrets.randbelow(10 ** 6):06d}"
    m.handover_code_hash = _code_hash(m.id, code)
    m.handover_code_expires_at = datetime.utcnow() + HANDOVER_TTL
    m.handover_attempts = 0
    from app.routers.transactions import _append_milestone_log
    _append_milestone_log(m, current_user.id, "handover_code_created", None)
    db.commit()
    return {"code": code, "expires_at": m.handover_code_expires_at.isoformat(),
            "message": "Give this code to the seller ONLY when you have received the goods. "
                       "Entering it releases this milestone's payment to them."}


@router.post("/{transaction_id}/milestones/{milestone_id}/confirm-handover",
             summary="Seller: enter the buyer's handover code to confirm receipt and release the milestone")
def confirm_handover(transaction_id: str, milestone_id: str, body: HandoverConfirmBody,
                     current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tx = _deal(db, transaction_id, current_user, lock=True)
    if current_user.id != tx.seller_id:
        raise HTTPException(status_code=403, detail="Only the seller enters the handover code")
    m = _milestone(db, tx, milestone_id)
    if m.status == "completed":
        return {"message": "This milestone has already been released.", "released": True}
    if m.status not in HANDOVER_STATUSES or not m.is_funded or tx.status in CLOSED_DEAL:
        raise HTTPException(status_code=409, detail={"error": "NOT_READY", "message": "This milestone can't be released now."})
    if _open_dispute(db, tx, m):
        raise HTTPException(status_code=409, detail={"error": "DISPUTED",
                            "message": "There's an open dispute on this milestone. It will be settled through the dispute."})
    if not m.handover_code_hash or not m.handover_code_expires_at or m.handover_code_expires_at < datetime.utcnow():
        raise HTTPException(status_code=409, detail={"error": "NO_ACTIVE_CODE",
                            "message": "There's no active handover code. Ask the buyer to create one in the app."})
    if (m.handover_attempts or 0) >= HANDOVER_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail={"error": "TOO_MANY_ATTEMPTS",
                            "message": "Too many wrong codes. Ask the buyer to create a new one."})
    code = "".join(ch for ch in (body.code or "") if ch.isdigit())
    if not hmac.compare_digest(_code_hash(m.id, code), m.handover_code_hash):
        m.handover_attempts = (m.handover_attempts or 0) + 1
        left = HANDOVER_MAX_ATTEMPTS - m.handover_attempts
        db.commit()
        raise HTTPException(status_code=422, detail={"error": "WRONG_CODE",
                            "message": f"That code isn't right. {left} attempt{'s' if left != 1 else ''} left."})

    from app.routers.transactions import _append_milestone_log, _release_milestone_escrow
    now = datetime.utcnow()
    m.handover_confirmed_at = now
    m.handover_code_hash = None
    m.delivered_at = m.delivered_at or now
    _append_milestone_log(m, current_user.id, "handover_confirmed", {"at": now.isoformat() + "Z"})
    try:
        _release_milestone_escrow(db, tx, m, "Receipt confirmed with the buyer's handover code")
    except InsufficientFunds:
        db.rollback()
        logger.error("Escrow shortfall on handover release %s", milestone_id)
        raise HTTPException(status_code=409, detail={"error": "ESCROW_MISMATCH",
                            "message": "The escrow for this milestone doesn't add up. Please contact support."})
    create_notification(db, tx.buyer_id, "Handover confirmed",
                        f"The seller confirmed handover of '{m.title}' with your code. Payment was released.",
                        "payment", related_item_id=tx.id, related_item_type="transaction")
    db.commit()
    return {"message": "Handover confirmed. Payment released.", "released": True,
            "handover_confirmed_at": now.isoformat()}


# ─── Public verification (no sign-in) ───────────────────────────────────────

public_router = APIRouter(prefix="/documents", tags=["deal documents"])


@public_router.get("/verify/{code}", summary="Check that a Bondrova receipt or contract is genuine")
def verify_document(code: str, db: Session = Depends(get_db)):
    out = dd.verify(db, code)
    if not out:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "valid": False,
                            "message": "No Bondrova document has this code. Check the code, or the document may not be genuine."})
    return out
