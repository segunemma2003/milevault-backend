"""
Funding deadline enforcement — callable from Celery tasks and from API handlers (lazy evaluation).
Returns a small status dict; mutates DB and commits are the caller's responsibility (this module commits internally for simplicity).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from sqlalchemy.orm import Session

from app.models.transaction import Transaction, Milestone
from app.models.wallet import WalletBalance, WalletTransaction, LedgerEntry
from app.services.notification_service import create_notification


def enforce_funding_deadline_if_due(db: Session, transaction_id: str) -> Dict[str, Any]:
    """
    If funding_deadline has passed and transaction is still in a funding phase, apply the same rules as the Celery task.
    Idempotent per transaction state. Commits on success paths.
    """
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        return {"status": "not_found", "commit": False}
    if tx.status in ("completed", "cancelled", "refunded"):
        return {"status": "skipped", "reason": tx.status, "commit": False}
    if tx.status not in ("funding_in_progress", "partially_funded", "active", "approved"):
        return {"status": "skipped", "reason": f"status={tx.status}", "commit": False}
    if not tx.funding_deadline or datetime.utcnow() < tx.funding_deadline:
        return {"status": "not_yet_due", "commit": False}

    milestones = db.query(Milestone).filter(Milestone.transaction_id == tx.id).all()
    any_fully_funded = any(m.is_funded for m in milestones)

    def refund_milestone_escrow_to_buyer(m: Milestone) -> float:
        amt = float(m.funded_amount or 0)
        if amt <= 0:
            return 0.0
        currency = m.currency or tx.currency
        buyer_balance = db.query(WalletBalance).filter(
            WalletBalance.user_id == tx.buyer_id,
            WalletBalance.currency == currency,
        ).first()
        if buyer_balance:
            buyer_balance.escrow_amount = max(0.0, round((buyer_balance.escrow_amount or 0) - amt, 8))
            buyer_balance.amount = round((buyer_balance.amount or 0) + amt, 8)
        db.add(
            LedgerEntry(
                debit_user_id=tx.buyer_id,
                credit_user_id=tx.buyer_id,
                debit_account="escrow",
                credit_account="available",
                amount=amt,
                currency=currency,
                reference_type="refund",
                reference_id=m.id,
                description=f"Funding deadline refund for milestone '{m.title}'",
            )
        )
        db.add(
            WalletTransaction(
                user_id=tx.buyer_id,
                type="escrow_refund",
                amount=amt,
                currency=currency,
                status="completed",
                transaction_id=tx.id,
                milestone_id=m.id,
                description=f"Deadline refund: {m.title}",
            )
        )
        m.funded_amount = 0.0
        m.is_funded = False
        m.status = "pending"
        return amt

    if not any_fully_funded:
        refunded = 0.0
        for m in milestones:
            refunded += refund_milestone_escrow_to_buyer(m)
        tx.funded_milestones = 0
        tx.status = "cancelled"
        tx.funding_deadline = None
        create_notification(
            db,
            tx.buyer_id,
            "Funding deadline — transaction cancelled",
            f"'{tx.title}' had no fully funded milestones by the deadline. Held funds were returned to your wallet.",
            "transaction",
            related_item_id=tx.id,
            related_item_type="transaction",
        )
        if tx.seller_id:
            create_notification(
                db,
                tx.seller_id,
                "Transaction cancelled (funding deadline)",
                f"'{tx.title}' was cancelled because no milestone was fully funded in time.",
                "transaction",
                related_item_id=tx.id,
                related_item_type="transaction",
            )
        from app.services.agent_fee_service import refund_held_agent_fees_for_transaction

        refund_held_agent_fees_for_transaction(db, tx)
        return {"status": "cancelled", "refunded": refunded, "commit": True}

    for m in milestones:
        if not m.is_funded and (m.funded_amount or 0) > 0:
            refund_milestone_escrow_to_buyer(m)
    tx.funding_deadline = None
    create_notification(
        db,
        tx.buyer_id,
        "Funding deadline — partial refunds",
        f"Unfunded portions on '{tx.title}' were returned to your wallet. Funded milestones are unchanged.",
        "transaction",
        related_item_id=tx.id,
        related_item_type="transaction",
    )
    return {"status": "partial_refunds", "transaction_id": transaction_id, "commit": True}
