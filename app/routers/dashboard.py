from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.transaction import Transaction
from app.models.wallet import WalletBalance, WalletTransaction
from app.models.dispute import Dispute
from app.models.user import User
from app.dependencies import get_current_user

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats")
def get_dashboard_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = current_user.id

    total = db.query(Transaction).filter(
        (Transaction.buyer_id == user_id) | (Transaction.seller_id == user_id)
    ).count()

    active_statuses = ["approved", "in_progress", "pending_approval"]
    active = db.query(Transaction).filter(
        ((Transaction.buyer_id == user_id) | (Transaction.seller_id == user_id)),
        Transaction.status.in_(active_statuses),
    ).count()

    completed = db.query(Transaction).filter(
        ((Transaction.buyer_id == user_id) | (Transaction.seller_id == user_id)),
        Transaction.status == "completed",
    ).count()

    pending_approval = db.query(Transaction).filter(
        ((Transaction.buyer_id == user_id) | (Transaction.seller_id == user_id)),
        Transaction.status == "pending_approval",
    ).count()

    total_disputes = db.query(Dispute).filter(Dispute.raised_by == user_id).count()

    balances = db.query(WalletBalance).filter(WalletBalance.user_id == user_id).all()
    wallet_balance = [
        {"currency": b.currency, "amount": b.amount}
        for b in balances
    ]

    pending_withdrawals = db.query(WalletTransaction).filter(
        WalletTransaction.user_id == user_id,
        WalletTransaction.type == "withdrawal",
        WalletTransaction.status == "pending",
    ).count()

    usd_balance = next((b.amount for b in balances if b.currency == "USD"), 0.0)

    # Get recent transactions
    recent_txns = db.query(Transaction).filter(
        (Transaction.buyer_id == user_id) | (Transaction.seller_id == user_id)
    ).order_by(Transaction.updated_at.desc()).limit(5).all()

    recent = []
    for tx in recent_txns:
        recent.append({
            "id": tx.id,
            "title": tx.title,
            "status": tx.status,
            "amount": tx.amount,
            "currency": tx.currency,
            "updated_at": tx.updated_at,
        })

    return {
        "total_transactions": total,
        "active_transactions": active,
        "completed_transactions": completed,
        "pending_approval": pending_approval,
        "total_disputes": total_disputes,
        "wallet_balance": wallet_balance,
        "pending_withdrawals": pending_withdrawals,
        "available_to_withdraw": usd_balance,
        "recent_transactions": recent,
    }
