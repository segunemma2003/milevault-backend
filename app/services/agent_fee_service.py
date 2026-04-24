"""
Agent verification fee: debit buyer on agent accept → platform hold (ledger).

Settlement when the transaction completes:
- Pay agent only if the agent request is **completed** (verification finished)
  OR **`admin_payout_approved`** is set (manual / dispute resolution).
- Otherwise refund the hold to the buyer (no passive payout for bare acceptance).

Dispute policy (admin): `agent_fee_action` on dispute resolution:
- `refund_buyer` — refund all held agent fees on the transaction to the buyer.
- `release_agent` — set admin approval on held earnings, then settle (pay agent).
- `None` — default settle rules above.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentEarning, AgentRequest, AgentRequestStatus
from app.models.transaction import Transaction
from app.models.wallet import LedgerEntry, WalletBalance, WalletTransaction


def _get_balance(db: Session, user_id: str, currency: str) -> WalletBalance:
    b = (
        db.query(WalletBalance)
        .filter(WalletBalance.user_id == user_id, WalletBalance.currency == currency)
        .first()
    )
    if not b:
        b = WalletBalance(user_id=user_id, currency=currency, amount=0.0)
        db.add(b)
        db.flush()
    return b


def hold_agent_fee_on_agent_accept(db: Session, req: AgentRequest) -> Optional[AgentEarning]:
    """
    Move fee from buyer available → platform (ledger). Idempotent if earning already held/paid.
    """
    if not req.fee_charged or float(req.fee_charged) <= 0:
        return None
    existing = (
        db.query(AgentEarning)
        .filter(AgentEarning.request_id == req.id, AgentEarning.status.in_(("held", "paid")))
        .first()
    )
    if existing:
        return existing

    currency = (req.fee_currency or "USD").upper()
    fee = round(float(req.fee_charged), 8)
    buyer_bal = _get_balance(db, str(req.buyer_id), currency)
    if (buyer_bal.amount or 0) < fee:
        raise ValueError(
            f"INSUFFICIENT_FUNDS: Need {currency} {fee:.2f} in wallet to cover the agent verification fee."
        )

    buyer_bal.amount = round((buyer_bal.amount or 0) - fee, 8)
    db.add(
        LedgerEntry(
            debit_user_id=str(req.buyer_id),
            credit_user_id=None,
            debit_account="available",
            credit_account="platform",
            amount=fee,
            currency=currency,
            reference_type="agent_fee_hold",
            reference_id=str(req.id),
            description="Agent verification fee held until verification completes or admin payout",
        )
    )
    db.add(
        WalletTransaction(
            user_id=str(req.buyer_id),
            type="agent_fee_hold",
            amount=fee,
            currency=currency,
            status="completed",
            transaction_id=str(req.transaction_id),
            description="Agent verification fee (held)",
        )
    )
    agent = db.query(Agent).filter(Agent.id == str(req.agent_id)).first()
    if not agent:
        raise ValueError("Agent not found for earning record")
    gross = float(req.fee_charged)
    payout = float(req.agent_payout_amount or 0)
    platform_cut = round(gross - payout, 8)
    earning = AgentEarning(
        agent_id=str(agent.id),
        request_id=str(req.id),
        gross_fee=gross,
        agent_payout=payout,
        platform_cut=platform_cut,
        currency=currency,
        status="held",
    )
    db.add(earning)
    return earning


def _refund_one_held_earning(db: Session, tx: Transaction, earning: AgentEarning, req: AgentRequest) -> None:
    currency = (earning.currency or "USD").upper()
    fee = round(float(earning.gross_fee), 8)
    buyer_bal = _get_balance(db, str(tx.buyer_id), currency)
    buyer_bal.amount = round((buyer_bal.amount or 0) + fee, 8)
    db.add(
        LedgerEntry(
            debit_user_id=None,
            credit_user_id=str(tx.buyer_id),
            debit_account="platform",
            credit_account="available",
            amount=fee,
            currency=currency,
            reference_type="agent_fee_refund",
            reference_id=str(req.id),
            description="Agent fee refunded (verification not completed / policy)",
        )
    )
    db.add(
        WalletTransaction(
            user_id=str(tx.buyer_id),
            type="agent_fee_refund",
            amount=fee,
            currency=currency,
            status="completed",
            transaction_id=str(tx.id),
            description="Agent verification fee refunded",
        )
    )
    earning.status = "refunded"


def refund_held_agent_fees_for_transaction(db: Session, tx: Transaction) -> int:
    """Refund every held agent fee on this transaction to the buyer (deterministic)."""
    refunded = 0
    earnings = (
        db.query(AgentEarning)
        .join(AgentRequest, AgentEarning.request_id == AgentRequest.id)
        .filter(AgentRequest.transaction_id == tx.id, AgentEarning.status == "held")
        .all()
    )
    for earning in earnings:
        req = db.query(AgentRequest).filter(AgentRequest.id == earning.request_id).first()
        if not req:
            continue
        _refund_one_held_earning(db, tx, earning, req)
        refunded += 1
    return refunded


def _payout_one_held_earning(db: Session, tx: Transaction, earning: AgentEarning) -> None:
    agent = db.query(Agent).filter(Agent.id == earning.agent_id).first()
    if not agent:
        return
    payout = round(float(earning.agent_payout), 8)
    currency = (earning.currency or "USD").upper()
    agent_user_id = str(agent.user_id)
    if payout <= 0:
        earning.status = "paid"
        earning.paid_at = datetime.utcnow()
        return
    bal = _get_balance(db, agent_user_id, currency)
    bal.amount = round((bal.amount or 0) + payout, 8)
    db.add(
        LedgerEntry(
            debit_user_id=None,
            credit_user_id=agent_user_id,
            debit_account="platform",
            credit_account="available",
            amount=payout,
            currency=currency,
            reference_type="agent_fee_payout",
            reference_id=str(earning.request_id),
            description="Agent verification fee released after eligibility confirmed",
        )
    )
    db.add(
        WalletTransaction(
            user_id=agent_user_id,
            type="agent_fee_payout",
            amount=payout,
            currency=currency,
            status="completed",
            transaction_id=str(tx.id),
            description="Agent verification fee payout",
        )
    )
    earning.status = "paid"
    earning.paid_at = datetime.utcnow()
    agent.total_earnings = round(float(agent.total_earnings or 0) + payout, 8)


def settle_held_agent_fees_on_transaction_completed(db: Session, tx: Transaction) -> Dict[str, Any]:
    """
    When transaction reaches `completed`: pay held fees only if agent completed verification
    or admin approved payout; otherwise refund buyer (prevents passive earnings abuse).
    """
    paid = 0
    refunded = 0
    earnings = (
        db.query(AgentEarning)
        .join(AgentRequest, AgentEarning.request_id == AgentRequest.id)
        .filter(AgentRequest.transaction_id == tx.id, AgentEarning.status == "held")
        .all()
    )
    for earning in earnings:
        req = db.query(AgentRequest).filter(AgentRequest.id == earning.request_id).first()
        if not req:
            continue
        eligible = bool(getattr(earning, "admin_payout_approved", False)) or req.status == AgentRequestStatus.completed
        if eligible:
            _payout_one_held_earning(db, tx, earning)
            paid += 1
        else:
            _refund_one_held_earning(db, tx, earning, req)
            refunded += 1
    return {"paid": paid, "refunded": refunded}


def apply_agent_fee_policy_after_dispute(
    db: Session,
    tx: Transaction,
    agent_fee_action: Optional[str],
) -> Dict[str, Any]:
    """
    After admin sets dispute to resolved (tx may be completed).
    - refund_buyer: refund all held agent fees.
    - release_agent: mark admin approval on all held earnings for this tx, then settle (pay).
    - None: default settle (complete vs refund per agent completion).
    """
    if not tx or tx.status != "completed":
        return {"skipped": True, "reason": "transaction_not_completed"}
    if agent_fee_action == "refund_buyer":
        n = refund_held_agent_fees_for_transaction(db, tx)
        return {"policy": "refund_buyer", "refunded_count": n}
    if agent_fee_action == "release_agent":
        held = (
            db.query(AgentEarning)
            .join(AgentRequest, AgentEarning.request_id == AgentRequest.id)
            .filter(AgentRequest.transaction_id == tx.id, AgentEarning.status == "held")
            .all()
        )
        for e in held:
            e.admin_payout_approved = True
            e.admin_payout_approved_at = datetime.utcnow()
        return {"policy": "release_agent", **settle_held_agent_fees_on_transaction_completed(db, tx)}
    return {"policy": "default", **settle_held_agent_fees_on_transaction_completed(db, tx)}


def refund_held_agent_fee_for_request(db: Session, req: AgentRequest) -> bool:
    """Refund the hold for one agent request (e.g. buyer cancelled the request)."""
    tx = db.query(Transaction).filter(Transaction.id == req.transaction_id).first()
    if not tx:
        return False
    earning = db.query(AgentEarning).filter(AgentEarning.request_id == req.id, AgentEarning.status == "held").first()
    if not earning:
        return False
    _refund_one_held_earning(db, tx, earning, req)
    return True


def admin_force_payout_held_earning(db: Session, earning: AgentEarning, admin_user_id: str) -> None:
    """
    Admin manual payout: marks approval and moves ledger funds if still held.
    Caller must commit.
    """
    if earning.status != "held":
        return
    req = db.query(AgentRequest).filter(AgentRequest.id == earning.request_id).first()
    if not req:
        return
    tx = db.query(Transaction).filter(Transaction.id == req.transaction_id).first()
    if not tx:
        return
    earning.admin_payout_approved = True
    earning.admin_payout_approved_at = datetime.utcnow()
    earning.admin_payout_approved_by = admin_user_id
    _payout_one_held_earning(db, tx, earning)


# Backwards-compatible name used by dispute hook when no explicit policy:
def release_held_agent_fees_after_dispute_resolved(db: Session, tx: Transaction) -> int:
    r = settle_held_agent_fees_on_transaction_completed(db, tx)
    return int(r.get("paid", 0))
