"""
Mutual cancellation of a deal that has started.

Before the invitation is accepted, the initiator can still withdraw it alone (DELETE /transactions/{id}).
Once it is accepted the deal has started and neither party can cancel it alone:

  1. Either party asks to cancel, with a reason          → request is `pending`
  2. The OTHER party accepts                             → deal `cancelled`, escrow refunded
     …or rejects (the deal carries on)                   → request `rejected`
     …or the requester takes it back                     → request `withdrawn`

On acceptance every milestone that has not been released gives its escrowed money (price + the buyer's
fee share, i.e. exactly what was escrowed) back to the buyer's available balance, and any held agent fee is
refunded. Milestones already released stay paid — that money belongs to the seller.
A deal under dispute can't be cancelled this way; the admin settles it through the dispute.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict

from sqlalchemy.orm import Session

from app.models.dispute import Dispute
from app.models.transaction import CancellationRequest, Milestone, Transaction
from app.models.wallet import WalletTransaction
from app.services import ledger

# Invitation not accepted yet — the initiator may still withdraw it alone.
PRE_START_STATUSES = ("draft", "pending_acceptance", "pending_approval")
FINAL_STATUSES = ("completed", "cancelled", "refunded", "declined", "expired")


class CancellationError(Exception):
    def __init__(self, status_code: int, error: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message


def has_started(tx: Transaction) -> bool:
    return tx.status not in PRE_START_STATUSES and tx.status not in FINAL_STATUSES


def pending_request(db: Session, tx_id: str) -> CancellationRequest | None:
    return (
        db.query(CancellationRequest)
        .filter(CancellationRequest.transaction_id == tx_id, CancellationRequest.status == "pending")
        .first()
    )


def other_party(tx: Transaction, user_id: str) -> str | None:
    return tx.seller_id if user_id == tx.buyer_id else tx.buyer_id


def _require_party(tx: Transaction, user_id: str) -> None:
    if user_id not in (tx.buyer_id, tx.seller_id):
        raise CancellationError(403, "ACCESS_DENIED", "Only the buyer or the seller of this deal can do this.")


def _has_open_dispute(db: Session, tx: Transaction) -> bool:
    if tx.status == "disputed":
        return True
    return (
        db.query(Dispute)
        .filter(Dispute.transaction_id == tx.id, Dispute.status.in_(("open", "in_review")))
        .first()
        is not None
    )


def create_request(db: Session, tx: Transaction, user_id: str, reason: str) -> CancellationRequest:
    _require_party(tx, user_id)
    if tx.status in FINAL_STATUSES:
        raise CancellationError(409, "DEAL_CLOSED", f"This deal is already {tx.status}.")
    if tx.status in PRE_START_STATUSES:
        raise CancellationError(
            409, "NOT_STARTED",
            "This invitation hasn't been accepted yet — the sender can cancel it, or you can decline it.",
        )
    if _has_open_dispute(db, tx):
        raise CancellationError(409, "DEAL_DISPUTED", "This deal is under dispute. It will be settled through the dispute.")
    if not tx.buyer_id or not tx.seller_id:
        raise CancellationError(409, "PARTY_MISSING", "Both parties must be on the deal.")
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise CancellationError(422, "REASON_REQUIRED", "Please give a reason for cancelling (at least 5 characters).")
    if pending_request(db, tx.id):
        raise CancellationError(409, "REQUEST_PENDING", "There is already a cancellation request waiting for a response.")

    req = CancellationRequest(transaction_id=tx.id, requested_by=user_id, reason=reason[:2000], status="pending")
    db.add(req)
    db.flush()
    return req


def _get_pending(db: Session, tx: Transaction) -> CancellationRequest:
    req = (
        db.query(CancellationRequest)
        .filter(CancellationRequest.transaction_id == tx.id, CancellationRequest.status == "pending")
        .with_for_update()
        .first()
    )
    if not req:
        raise CancellationError(404, "NO_PENDING_REQUEST", "There is no cancellation request waiting for a response.")
    return req


def _close(req: CancellationRequest, status: str, user_id: str, note: str | None) -> None:
    req.status = status
    req.responded_by = user_id
    req.response_note = (note or "").strip()[:2000] or None
    req.responded_at = datetime.utcnow()


def withdraw_request(db: Session, tx: Transaction, user_id: str) -> CancellationRequest:
    _require_party(tx, user_id)
    req = _get_pending(db, tx)
    if req.requested_by != user_id:
        raise CancellationError(403, "NOT_REQUESTER", "Only the person who asked to cancel can withdraw the request.")
    _close(req, "withdrawn", user_id, None)
    return req


def reject_request(db: Session, tx: Transaction, user_id: str, note: str | None) -> CancellationRequest:
    _require_party(tx, user_id)
    req = _get_pending(db, tx)
    if req.requested_by == user_id:
        raise CancellationError(403, "OWN_REQUEST", "The other party has to respond to your request. You can withdraw it instead.")
    _close(req, "rejected", user_id, note)
    return req


def refund_unreleased_escrow(db: Session, tx: Transaction) -> Dict[str, float]:
    """
    Return every unreleased milestone's escrow to the buyer. Exactly once per milestone
    (idempotency key), and only what was escrowed FOR THIS DEAL — never the buyer's other escrow.
    Returns {currency: amount_refunded}.
    """
    refunded: Dict[str, float] = {}
    milestones = (
        db.query(Milestone)
        .filter(Milestone.transaction_id == tx.id)
        .with_for_update()
        .all()
    )
    for m in milestones:
        if m.status == "completed":
            continue  # released to the seller — stays paid
        amt = float(m.funded_amount or 0)
        currency = (m.currency or tx.currency).upper()
        if amt > 0:
            applied = ledger.move(
                db, user_id=tx.buyer_id, currency=currency, amount=amt,
                from_bucket="escrow", to_bucket="available",
                reference_type="cancellation_refund", reference_id=m.id,
                description=f"Refund — deal cancelled by agreement: '{m.title}'",
                idempotency_key=m.id,
            )
            if applied:
                db.add(WalletTransaction(
                    user_id=tx.buyer_id, type="escrow_refund", amount=amt, currency=currency,
                    status="completed", transaction_id=tx.id, milestone_id=m.id,
                    description=f"Refund (deal cancelled): {m.title}",
                ))
                refunded[currency] = round(refunded.get(currency, 0.0) + amt, 8)
        m.funded_amount = 0.0
        m.is_funded = False
        m.auto_release_at = None
        m.status = "cancelled"
    return refunded


def accept_request(db: Session, tx: Transaction, user_id: str, note: str | None) -> Dict[str, float]:
    """The other party agrees: cancel the deal and refund the buyer. Raises InsufficientFunds if the
    buyer's escrow can't cover this deal's funded milestones (caller rolls back)."""
    _require_party(tx, user_id)
    req = _get_pending(db, tx)
    if req.requested_by == user_id:
        raise CancellationError(403, "OWN_REQUEST", "The other party has to accept your request.")
    if tx.status in FINAL_STATUSES:
        raise CancellationError(409, "DEAL_CLOSED", f"This deal is already {tx.status}.")
    if _has_open_dispute(db, tx):
        raise CancellationError(409, "DEAL_DISPUTED", "This deal is under dispute. It will be settled through the dispute.")

    refunded = refund_unreleased_escrow(db, tx)

    from app.services.agent_fee_service import refund_held_agent_fees_for_transaction
    refund_held_agent_fees_for_transaction(db, tx)

    _close(req, "accepted", user_id, note)
    tx.status = "cancelled"
    tx.funded_milestones = 0
    tx.funding_deadline = None
    if refunded:
        from app.services.deal_documents import queue_receipt_email
        queue_receipt_email(db, tx, "The deal was cancelled by agreement and the money still in escrow was returned to the buyer.")
    return refunded


def request_to_dict(req: CancellationRequest | None) -> dict | None:
    if not req:
        return None
    return {
        "id": req.id,
        "requested_by": req.requested_by,
        "reason": req.reason,
        "status": req.status,
        "responded_by": req.responded_by,
        "response_note": req.response_note,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "responded_at": req.responded_at.isoformat() if req.responded_at else None,
    }
