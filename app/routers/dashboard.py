from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.transaction import Transaction
from app.models.wallet import WalletBalance, WalletTransaction
from app.models.dispute import Dispute
from app.models.user import User
from app.dependencies import get_current_user

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


FINAL_STATUSES = ("completed", "cancelled", "refunded", "declined", "expired")
AWAITING_RESPONSE = ("pending_approval", "pending_acceptance")


@router.get("/stats")
def get_dashboard_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Dashboard summary. Money per currency (the app converts to the viewer's display currency):
      available — free to use or withdraw
      in_escrow — your money locked in deals you're paying for (buyer)
      incoming  — money held in escrow for you in deals you're delivering (seller)
      pending   — withdrawals on their way out
    """
    from app.models.transaction import Milestone
    user_id = current_user.id
    mine = (Transaction.buyer_id == user_id) | (Transaction.seller_id == user_id)

    total = db.query(Transaction).filter(mine).count()
    active = db.query(Transaction).filter(mine, Transaction.status.notin_(FINAL_STATUSES + AWAITING_RESPONSE)).count()
    completed = db.query(Transaction).filter(mine, Transaction.status == "completed").count()
    pending_approval = db.query(Transaction).filter(mine, Transaction.status.in_(AWAITING_RESPONSE)).count()
    paused = db.query(Transaction).filter(mine, Transaction.status == "awaiting_funding").count()

    open_disputes = (
        db.query(Dispute).join(Transaction, Dispute.transaction_id == Transaction.id)
        .filter(mine, Dispute.status.in_(("open", "in_review"))).count()
    )

    balances = db.query(WalletBalance).filter(WalletBalance.user_id == user_id).all()
    wallet_balance = [
        {"currency": b.currency, "amount": float(b.amount or 0), "escrow": float(b.escrow_amount or 0),
         "pending": float(b.pending_amount or 0)}
        for b in balances
    ]

    # Held for this user as a seller: escrowed on open milestones of their deals (price only —
    # the buyer's fee share isn't theirs).
    incoming: dict = {}
    rows = (
        db.query(Milestone, Transaction.currency)
        .join(Transaction, Milestone.transaction_id == Transaction.id)
        .filter(Transaction.seller_id == user_id, Milestone.status != "completed",
                Transaction.status.notin_(FINAL_STATUSES), Milestone.funded_amount > 0)
        .all()
    )
    for m, tx_cur in rows:
        cur = (m.currency or tx_cur or "USD").upper()
        held = min(float(m.funded_amount or 0), float(m.amount or 0)) - float(m.seller_fee or 0) * (1 if m.is_funded else 0)
        incoming[cur] = round(incoming.get(cur, 0.0) + max(held, 0.0), 2)

    pending_withdrawals = db.query(WalletTransaction).filter(
        WalletTransaction.user_id == user_id,
        WalletTransaction.type == "withdrawal",
        WalletTransaction.status.in_(("pending", "processing")),
    ).count()

    usd_balance = next((float(b.amount or 0) for b in balances if b.currency == "USD"), 0.0)

    recent_txns = db.query(Transaction).filter(mine).order_by(Transaction.updated_at.desc()).limit(5).all()
    recent = [{"id": tx.id, "title": tx.title, "status": tx.status, "amount": tx.amount,
               "currency": tx.currency, "updated_at": tx.updated_at} for tx in recent_txns]

    if current_user.trust_score is None:
        from app.services import trust_score
        trust_score.recompute(db, current_user, "First calculation")
        db.commit()

    return {
        "trust_score": current_user.trust_score,
        "total_transactions": total,
        "active_transactions": active,
        "completed_transactions": completed,
        "pending_approval": pending_approval,
        "paused_transactions": paused,
        "open_disputes": open_disputes,
        "total_disputes": open_disputes,  # kept for older clients
        "wallet_balance": wallet_balance,
        "incoming_escrow": [{"currency": c, "amount": a} for c, a in incoming.items()],
        "pending_withdrawals": pending_withdrawals,
        "available_to_withdraw": usd_balance,
        "recent_transactions": recent,
    }


@router.get("/analytics")
def get_user_analytics(
    days: int = Query(30, ge=7, le=90),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the authenticated user's own time-series analytics for the last N days."""
    user_id = current_user.id
    now = datetime.utcnow()
    start = now - timedelta(days=days)

    # Build a date-keyed scaffold so every day appears even with zero values
    date_range = [(start + timedelta(days=i)).date() for i in range(days + 1)]
    scaffold = {d: 0.0 for d in date_range}

    # --- Transaction volume (sum of amounts by day) ---
    tx_rows = (
        db.query(
            func.date(Transaction.created_at).label("day"),
            func.coalesce(func.sum(Transaction.amount), 0).label("total"),
        )
        .filter(
            (Transaction.buyer_id == user_id) | (Transaction.seller_id == user_id),
            Transaction.created_at >= start,
        )
        .group_by(func.date(Transaction.created_at))
        .all()
    )
    tx_by_day = {**scaffold}
    for row in tx_rows:
        tx_by_day[row.day] = float(row.total)

    # --- Wallet deposits by day ---
    dep_rows = (
        db.query(
            func.date(WalletTransaction.created_at).label("day"),
            func.coalesce(func.sum(WalletTransaction.amount), 0).label("total"),
        )
        .filter(
            WalletTransaction.user_id == user_id,
            WalletTransaction.type == "deposit",
            WalletTransaction.status == "completed",
            WalletTransaction.created_at >= start,
        )
        .group_by(func.date(WalletTransaction.created_at))
        .all()
    )
    dep_by_day = {**scaffold}
    for row in dep_rows:
        dep_by_day[row.day] = float(row.total)

    # --- Wallet withdrawals by day ---
    wd_rows = (
        db.query(
            func.date(WalletTransaction.created_at).label("day"),
            func.coalesce(func.sum(WalletTransaction.amount), 0).label("total"),
        )
        .filter(
            WalletTransaction.user_id == user_id,
            WalletTransaction.type == "withdrawal",
            WalletTransaction.created_at >= start,
        )
        .group_by(func.date(WalletTransaction.created_at))
        .all()
    )
    wd_by_day = {**scaffold}
    for row in wd_rows:
        wd_by_day[row.day] = float(row.total)

    def _fmt(d):
        return d.strftime("%b %-d")

    series = [
        {
            "date": str(d),
            "label": _fmt(d),
            "transaction_volume": tx_by_day.get(d, 0.0),
            "deposits": dep_by_day.get(d, 0.0),
            "withdrawals": wd_by_day.get(d, 0.0),
        }
        for d in date_range
    ]

    return {"days": days, "series": series}
