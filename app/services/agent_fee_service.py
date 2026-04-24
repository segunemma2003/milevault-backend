"""
Agent verification fee: debit buyer on agent accept → platform hold (ledger),
release to agent wallet when transaction completes (or dispute resolved to completed),
refund buyer on transaction cancel before payout.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentEarning, AgentRequest
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
    Returns AgentEarning row or None when no fee.
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
            description="Agent verification fee held until transaction completes",
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


def refund_held_agent_fees_for_transaction(db: Session, tx: Transaction) -> int:
    """Return held agent fees to buyer when deal ends without completion payout. Count refunded."""
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
                description="Agent fee refunded (transaction cancelled or not completed)",
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
        refunded += 1
    return refunded


def release_held_agent_fees_for_transaction(db: Session, tx: Transaction) -> int:
    """
    Pay agents for held earnings when transaction is completed (or admin resolved dispute to completed).
    Idempotent: skips earnings already paid.
    """
    paid = 0
    earnings = (
        db.query(AgentEarning)
        .join(AgentRequest, AgentEarning.request_id == AgentRequest.id)
        .filter(AgentRequest.transaction_id == tx.id, AgentEarning.status == "held")
        .all()
    )
    for earning in earnings:
        agent = db.query(Agent).filter(Agent.id == earning.agent_id).first()
        if not agent:
            continue
        payout = round(float(earning.agent_payout), 8)
        if payout <= 0:
            earning.status = "paid"
            earning.paid_at = datetime.utcnow()
            paid += 1
            continue
        currency = (earning.currency or "USD").upper()
        agent_user_id = str(agent.user_id)
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
                description="Agent verification fee released after transaction completion",
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
        paid += 1
    return paid


def refund_held_agent_fee_for_request(db: Session, req: AgentRequest) -> bool:
    """Refund the hold for one agent request (e.g. buyer cancelled the request)."""
    tx = db.query(Transaction).filter(Transaction.id == req.transaction_id).first()
    if not tx:
        return False
    earning = db.query(AgentEarning).filter(AgentEarning.request_id == req.id, AgentEarning.status == "held").first()
    if not earning:
        return False
    currency = (earning.currency or "USD").upper()
    fee = round(float(earning.gross_fee), 8)
    buyer_bal = _get_balance(db, str(req.buyer_id), currency)
    buyer_bal.amount = round((buyer_bal.amount or 0) + fee, 8)
    db.add(
        LedgerEntry(
            debit_user_id=None,
            credit_user_id=str(req.buyer_id),
            debit_account="platform",
            credit_account="available",
            amount=fee,
            currency=currency,
            reference_type="agent_fee_refund",
            reference_id=str(req.id),
            description="Agent fee refunded (agent request cancelled)",
        )
    )
    db.add(
        WalletTransaction(
            user_id=str(req.buyer_id),
            type="agent_fee_refund",
            amount=fee,
            currency=currency,
            status="completed",
            transaction_id=str(tx.id),
            description="Agent verification fee refunded (request cancelled)",
        )
    )
    earning.status = "refunded"
    return True


def release_held_agent_fees_after_dispute_resolved(db: Session, tx: Transaction) -> int:
    """When admin marks dispute resolved (tx may be set completed), release held agent fees."""
    if tx.status != "completed":
        return 0
    return release_held_agent_fees_for_transaction(db, tx)
