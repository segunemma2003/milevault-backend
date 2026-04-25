"""
Admin router — full platform control panel.
All endpoints require is_admin=True.
Covers: currencies, exchange rates, payment gateways, agent approval, refunds, transaction oversight.
"""
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status, Body
from sqlalchemy.orm import Session
from app.database import get_db
from app.dependencies import get_current_admin
from app.models.user import User
from app.models.currency import Currency, ExchangeRate, PaymentGateway, Refund, CurrencyType, PaymentGatewayName, PlatformSettings
from app.models.agent import (
    Agent,
    AgentStatus,
    AgentRequest,
    AgentRequestMessage,
    AgentRequestStatus,
    AgentSubscriptionPlan,
    AgentSubscription,
    AgentServiceTier,
    AgentEarning,
)
from app.models.transaction import Transaction, Milestone
from app.models.dispute import Dispute, DisputeDocument
from app.models.message import ChatMessage
from app.models.wallet import WalletBalance, WalletTransaction
from app.models.notification import Notification
from app.services.cache_service import cache_delete

router = APIRouter(prefix="/admin", tags=["Admin"])


def _notify(db: Session, user_id: str, title: str, message: str):
    db.add(Notification(user_id=str(user_id), title=title, message=message, type="admin"))


# ══════════════════════════════════════════════════════════════════
#  OVERVIEW
# ══════════════════════════════════════════════════════════════════

@router.get("/overview", summary="Platform overview stats")
def overview(db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    from sqlalchemy import func
    total_users = db.query(User).count()
    total_txns = db.query(Transaction).count()
    pending_agents = db.query(Agent).filter(Agent.status == AgentStatus.pending).count()
    open_disputes = db.query(Dispute).filter(Dispute.status == "open").count()
    pending_refunds = db.query(Refund).filter(Refund.status == "pending").count()
    pending_withdrawals = db.query(WalletTransaction).filter(
        WalletTransaction.type == "withdrawal",
        WalletTransaction.status == "pending",
    ).count()
    return {
        "total_users": total_users,
        "total_transactions": total_txns,
        "pending_agent_applications": pending_agents,
        "open_disputes": open_disputes,
        "pending_refunds": pending_refunds,
        "pending_withdrawals": pending_withdrawals,
    }


@router.get("/analytics", summary="Platform-wide time-series analytics")
def admin_analytics(
    days: int = Query(30, ge=7, le=90),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Return daily platform metrics for the last N days."""
    from sqlalchemy import func as sqlfunc
    now = datetime.utcnow()
    start = now - timedelta(days=days)

    date_range = [(start + timedelta(days=i)).date() for i in range(days + 1)]
    scaffold = {d: 0.0 for d in date_range}

    # Transaction volume (USD equivalent amounts) by day
    tx_rows = (
        db.query(
            sqlfunc.date(Transaction.created_at).label("day"),
            sqlfunc.coalesce(sqlfunc.sum(Transaction.amount), 0).label("total"),
        )
        .filter(Transaction.created_at >= start)
        .group_by(sqlfunc.date(Transaction.created_at))
        .all()
    )
    tx_by_day = {**scaffold}
    for row in tx_rows:
        tx_by_day[row.day] = float(row.total)

    # Withdrawal volume by day
    wd_rows = (
        db.query(
            sqlfunc.date(WalletTransaction.created_at).label("day"),
            sqlfunc.coalesce(sqlfunc.sum(WalletTransaction.amount), 0).label("total"),
        )
        .filter(
            WalletTransaction.type == "withdrawal",
            WalletTransaction.created_at >= start,
        )
        .group_by(sqlfunc.date(WalletTransaction.created_at))
        .all()
    )
    wd_by_day = {**scaffold}
    for row in wd_rows:
        wd_by_day[row.day] = float(row.total)

    # Disputes opened per day
    disp_rows = (
        db.query(
            sqlfunc.date(Dispute.created_at).label("day"),
            sqlfunc.count(Dispute.id).label("cnt"),
        )
        .filter(Dispute.created_at >= start)
        .group_by(sqlfunc.date(Dispute.created_at))
        .all()
    )
    disp_scaffold = {d: 0 for d in date_range}
    for row in disp_rows:
        disp_scaffold[row.day] = int(row.cnt)

    # KYC verifications by day
    kyc_rows = (
        db.query(
            sqlfunc.date(User.created_at).label("day"),
            sqlfunc.count(User.id).label("cnt"),
        )
        .filter(User.is_kyc_verified == True, User.created_at >= start)
        .group_by(sqlfunc.date(User.created_at))
        .all()
    )
    kyc_scaffold = {d: 0 for d in date_range}
    for row in kyc_rows:
        kyc_scaffold[row.day] = int(row.cnt)

    # New user registrations by day
    user_rows = (
        db.query(
            sqlfunc.date(User.created_at).label("day"),
            sqlfunc.count(User.id).label("cnt"),
        )
        .filter(User.created_at >= start)
        .group_by(sqlfunc.date(User.created_at))
        .all()
    )
    user_scaffold = {d: 0 for d in date_range}
    for row in user_rows:
        user_scaffold[row.day] = int(row.cnt)

    def _fmt(d):
        return d.strftime("%b %-d")

    series = [
        {
            "date": str(d),
            "label": _fmt(d),
            "transaction_volume": tx_by_day.get(d, 0.0),
            "withdrawal_volume": wd_by_day.get(d, 0.0),
            "disputes_opened": disp_scaffold.get(d, 0),
            "kyc_verified": kyc_scaffold.get(d, 0),
            "new_users": user_scaffold.get(d, 0),
        }
        for d in date_range
    ]

    return {"days": days, "series": series}


# ══════════════════════════════════════════════════════════════════
#  CURRENCY MANAGEMENT
# ══════════════════════════════════════════════════════════════════

@router.get("/currencies", summary="List all currencies")
def list_currencies(db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    currencies = db.query(Currency).order_by(Currency.code).all()
    return [
        {
            "id": str(c.id),
            "code": c.code,
            "name": c.name,
            "symbol": c.symbol,
            "type": c.type,
            "decimal_places": c.decimal_places,
            "is_active": c.is_active,
            "is_base": c.is_base,
        }
        for c in currencies
    ]


@router.post("/currencies", status_code=201, summary="Add a new currency")
def add_currency(
    code: str = Body(...),
    name: str = Body(...),
    symbol: str = Body(...),
    currency_type: str = Body("fiat"),
    decimal_places: int = Body(2),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    code = code.upper().strip()
    if db.query(Currency).filter(Currency.code == code).first():
        raise HTTPException(
            status_code=409,
            detail={"error": "CURRENCY_EXISTS", "message": f"Currency '{code}' already exists."},
        )
    try:
        ctype = CurrencyType(currency_type)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_TYPE", "message": "currency_type must be 'fiat' or 'crypto'."},
        )

    currency = Currency(
        code=code,
        name=name.strip(),
        symbol=symbol.strip(),
        type=ctype,
        decimal_places=decimal_places,
        created_by=str(admin.id),
    )
    db.add(currency)
    db.commit()
    cache_delete("currencies:active")
    return {"message": f"Currency '{code}' added.", "id": str(currency.id)}


@router.put("/currencies/{currency_id}", summary="Update currency (activate/deactivate)")
def update_currency(
    currency_id: str,
    is_active: Optional[bool] = Body(None),
    name: Optional[str] = Body(None),
    symbol: Optional[str] = Body(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    currency = db.query(Currency).filter(Currency.id == currency_id).first()
    if not currency:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Currency not found."})
    if is_active is not None:
        currency.is_active = is_active
    if name:
        currency.name = name
    if symbol:
        currency.symbol = symbol
    db.commit()
    cache_delete("currencies:active")
    return {"message": "Currency updated."}


# ══════════════════════════════════════════════════════════════════
#  EXCHANGE RATES
# ══════════════════════════════════════════════════════════════════

@router.get("/exchange-rates", summary="List all exchange rates")
def list_exchange_rates(db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    rates = db.query(ExchangeRate).all()
    return [
        {
            "id": str(r.id),
            "from_currency": r.from_currency.code if r.from_currency else None,
            "to_currency": r.to_currency.code if r.to_currency else None,
            "rate": r.rate,
            "spread_percent": r.spread_percent,
            "is_active": r.is_active,
            "set_by": f"{r.setter.first_name} {r.setter.last_name}".strip() if r.setter else None,
            "valid_from": r.valid_from.isoformat() if r.valid_from else None,
            "valid_to": r.valid_to.isoformat() if r.valid_to else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rates
    ]


@router.post("/exchange-rates", status_code=201, summary="Set an exchange rate")
def set_exchange_rate(
    from_code: str = Body(...),
    to_code: str = Body(...),
    rate: float = Body(...),
    spread_percent: float = Body(0.5),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    from_cur = db.query(Currency).filter(Currency.code == from_code.upper()).first()
    to_cur = db.query(Currency).filter(Currency.code == to_code.upper()).first()
    if not from_cur:
        raise HTTPException(
            status_code=404,
            detail={"error": "FROM_CURRENCY_NOT_FOUND", "message": f"Currency '{from_code}' not found. Add it first via /admin/currencies."},
        )
    if not to_cur:
        raise HTTPException(
            status_code=404,
            detail={"error": "TO_CURRENCY_NOT_FOUND", "message": f"Currency '{to_code}' not found. Add it first via /admin/currencies."},
        )
    if rate <= 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_RATE", "message": "Exchange rate must be greater than 0."},
        )

    existing = db.query(ExchangeRate).filter(
        ExchangeRate.from_currency_id == from_cur.id,
        ExchangeRate.to_currency_id == to_cur.id,
    ).first()

    if existing:
        existing.rate = rate
        existing.spread_percent = spread_percent
        existing.set_by = str(admin.id)
        existing.valid_from = datetime.utcnow()
        existing.valid_to = None
        existing.is_active = True
        db.commit()
        cache_delete(f"rate:{from_code}:{to_code}")
        return {"message": f"Rate {from_code}→{to_code} updated to {rate}."}
    else:
        er = ExchangeRate(
            from_currency_id=from_cur.id,
            to_currency_id=to_cur.id,
            rate=rate,
            spread_percent=spread_percent,
            set_by=str(admin.id),
        )
        db.add(er)
        db.commit()
        cache_delete(f"rate:{from_code}:{to_code}")
        return {"message": f"Rate {from_code}→{to_code} set to {rate}.", "id": str(er.id)}


@router.delete("/exchange-rates/{rate_id}", summary="Deactivate an exchange rate")
def deactivate_exchange_rate(
    rate_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    er = db.query(ExchangeRate).filter(ExchangeRate.id == rate_id).first()
    if not er:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Exchange rate not found."})
    er.is_active = False
    er.valid_to = datetime.utcnow()
    db.commit()
    return {"message": "Exchange rate deactivated."}


# ══════════════════════════════════════════════════════════════════
#  PAYMENT GATEWAYS
# ══════════════════════════════════════════════════════════════════

@router.get("/payment-gateways", summary="List configured payment gateways")
def list_gateways(db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    gws = db.query(PaymentGateway).all()
    return [
        {
            "id": str(g.id),
            "name": g.name,
            "display_name": g.display_name,
            "country_codes": g.country_codes,
            "supported_currencies": g.supported_currencies,
            "is_active": g.is_active,
            "supports_deposit": g.supports_deposit,
            "supports_withdrawal": g.supports_withdrawal,
            "min_amount": g.min_amount,
            "max_amount": g.max_amount,
            "fee_percent": g.fee_percent,
            "fee_fixed": g.fee_fixed,
        }
        for g in gws
    ]


@router.post("/payment-gateways", status_code=201, summary="Add/configure a payment gateway")
def add_gateway(
    name: str = Body(...),
    display_name: str = Body(...),
    country_codes: list = Body(...),
    supported_currencies: list = Body(...),
    fee_percent: float = Body(1.5),
    fee_fixed: float = Body(0.0),
    min_amount: float = Body(1.0),
    max_amount: float = Body(100000.0),
    supports_deposit: bool = Body(True),
    supports_withdrawal: bool = Body(True),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    try:
        gw_name = PaymentGatewayName(name.lower())
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "INVALID_GATEWAY",
                "message": f"'{name}' is not supported. Valid gateways: {[g.value for g in PaymentGatewayName]}",
            },
        )

    gw = PaymentGateway(
        name=gw_name,
        display_name=display_name,
        country_codes=[c.upper() for c in country_codes],
        supported_currencies=[c.upper() for c in supported_currencies],
        fee_percent=fee_percent,
        fee_fixed=fee_fixed,
        min_amount=min_amount,
        max_amount=max_amount,
        supports_deposit=supports_deposit,
        supports_withdrawal=supports_withdrawal,
    )
    db.add(gw)
    db.commit()
    return {"message": f"Gateway '{name}' added.", "id": str(gw.id)}


@router.put("/payment-gateways/{gateway_id}", summary="Update gateway settings")
def update_gateway(
    gateway_id: str,
    is_active: Optional[bool] = Body(None),
    fee_percent: Optional[float] = Body(None),
    min_amount: Optional[float] = Body(None),
    max_amount: Optional[float] = Body(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    gw = db.query(PaymentGateway).filter(PaymentGateway.id == gateway_id).first()
    if not gw:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Payment gateway not found."})
    if is_active is not None:
        gw.is_active = is_active
    if fee_percent is not None:
        gw.fee_percent = fee_percent
    if min_amount is not None:
        gw.min_amount = min_amount
    if max_amount is not None:
        gw.max_amount = max_amount
    db.commit()
    return {"message": "Gateway updated."}


# ══════════════════════════════════════════════════════════════════
#  AGENT MANAGEMENT
# ══════════════════════════════════════════════════════════════════

@router.get("/agents/pending", summary="List pending agent applications")
def list_pending_agents(db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    agents = db.query(Agent).filter(Agent.status == AgentStatus.pending).all()
    return [
        {
            "id": str(a.id),
            "user_id": str(a.user_id),
            "name": f"{a.user.first_name} {a.user.last_name}".strip() if a.user else "Unknown",
            "email": a.user.email if a.user else "Unknown",
            "specialty": a.specialty,
            "specialty_details": a.specialty_details,
            "years_experience": a.years_experience,
            "hourly_rate": a.hourly_rate,
            "hourly_rate_currency": a.hourly_rate_currency,
            "portfolio_url": a.portfolio_url,
            "id_document_url": a.id_document_url,
            "applied_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in agents
    ]


@router.put("/agents/{agent_id}/approve", summary="Approve an agent application")
def approve_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Agent not found."})
    if agent.status == AgentStatus.approved:
        raise HTTPException(status_code=409, detail={"error": "ALREADY_APPROVED", "message": "Agent is already approved."})

    agent.status = AgentStatus.approved
    agent.approved_by = str(admin.id)
    agent.approved_at = datetime.utcnow()
    agent.rejection_reason = None

    # Grant agent flag to the user account
    user = db.query(User).filter(User.id == str(agent.user_id)).first()
    if user:
        user.is_agent = True

    _notify(
        db, str(agent.user_id),
        "Agent Application Approved!",
        "Congratulations! Your agent application has been approved. You can now accept verification requests from buyers.",
    )
    db.commit()
    agent_name = f"{agent.user.first_name} {agent.user.last_name}".strip() if agent.user else agent_id
    return {"message": f"Agent '{agent_name}' approved."}


@router.put("/agents/{agent_id}/reject", summary="Reject an agent application")
def reject_agent(
    agent_id: str,
    reason: str = Body(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    if len(reason.strip()) < 20:
        raise HTTPException(
            status_code=422,
            detail={"error": "REASON_TOO_SHORT", "message": "Please provide a detailed rejection reason (min 20 characters)."},
        )

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Agent not found."})

    agent.status = AgentStatus.rejected
    agent.rejection_reason = reason.strip()

    user = db.query(User).filter(User.id == str(agent.user_id)).first()
    if user:
        user.is_agent = False

    _notify(
        db, str(agent.user_id),
        "Agent Application Rejected",
        f"Your agent application was not approved. Reason: {reason.strip()}. You may re-apply after addressing the concerns.",
    )
    db.commit()
    return {"message": "Agent application rejected."}


@router.put("/agents/{agent_id}/suspend", summary="Suspend an approved agent")
def suspend_agent(
    agent_id: str,
    reason: str = Body(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    agent = db.query(Agent).filter(Agent.id == agent_id, Agent.status == AgentStatus.approved).first()
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Approved agent not found."})

    agent.status = AgentStatus.suspended
    agent.rejection_reason = reason.strip()
    user = db.query(User).filter(User.id == str(agent.user_id)).first()
    if user:
        user.is_agent = False

    _notify(db, str(agent.user_id), "Agent Account Suspended", f"Your agent account has been suspended. Reason: {reason}")
    db.commit()
    return {"message": "Agent suspended."}


# ══════════════════════════════════════════════════════════════════
#  REFUND MANAGEMENT (Admin-only, after dispute review)
# ══════════════════════════════════════════════════════════════════

@router.get("/refunds", summary="List all refund requests")
def list_refunds(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    q = db.query(Refund)
    if status_filter:
        q = q.filter(Refund.status == status_filter)
    refunds = q.order_by(Refund.created_at.desc()).all()
    return [
        {
            "id": str(r.id),
            "transaction_id": str(r.transaction_id),
            "dispute_id": str(r.dispute_id) if r.dispute_id else None,
            "milestone_id": str(r.milestone_id) if getattr(r, "milestone_id", None) else None,
            "amount": r.amount,
            "currency": r.currency,
            "refund_to": str(r.refund_to),
            "recipient_name": f"{r.recipient.first_name} {r.recipient.last_name}".strip() if r.recipient else None,
            "reason": r.reason,
            "admin_notes": r.admin_notes,
            "status": r.status,
            "processed_at": r.processed_at.isoformat() if r.processed_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in refunds
    ]


@router.post("/refunds", status_code=201, summary="Admin creates a refund after dispute review")
def create_refund(
    transaction_id: str = Body(...),
    dispute_id: Optional[str] = Body(None),
    milestone_id: Optional[str] = Body(None),
    refund_to_user_id: str = Body(...),
    amount: float = Body(...),
    currency: str = Body(...),
    reason: str = Body(...),
    admin_notes: Optional[str] = Body(None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """
    Admin reviews dispute and creates a refund. Only admin can do this.
    The Celery task 'process_refund' will actually move the funds.
    """
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail={"error": "TRANSACTION_NOT_FOUND", "message": "Transaction not found."})

    recipient = db.query(User).filter(User.id == refund_to_user_id).first()
    if not recipient:
        raise HTTPException(
            status_code=404,
            detail={"error": "USER_NOT_FOUND", "message": "Refund recipient user not found."},
        )

    if amount <= 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_AMOUNT", "message": "Refund amount must be greater than 0."},
        )
    if amount > tx.amount:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "AMOUNT_EXCEEDS_TRANSACTION",
                "message": f"Refund amount ({amount}) cannot exceed the transaction amount ({tx.amount}).",
            },
        )

    if milestone_id:
        from app.models.transaction import Milestone

        ms = (
            db.query(Milestone)
            .filter(Milestone.id == milestone_id, Milestone.transaction_id == transaction_id)
            .first()
        )
        if not ms:
            raise HTTPException(
                status_code=422,
                detail={"error": "INVALID_MILESTONE", "message": "milestone_id does not belong to this transaction."},
            )

    refund = Refund(
        transaction_id=transaction_id,
        dispute_id=dispute_id,
        milestone_id=milestone_id,
        amount=amount,
        currency=currency.upper(),
        refund_to=refund_to_user_id,
        reason=reason.strip(),
        admin_notes=admin_notes,
        processed_by=str(admin.id),
        status="pending",
    )
    db.add(refund)
    db.flush()

    # Queue Celery task to process the actual fund movement
    from app.services.tasks import process_refund
    process_refund.delay(str(refund.id))

    _notify(
        db, refund_to_user_id,
        "Refund Initiated",
        f"A refund of {currency.upper()} {amount:,.2f} has been initiated for your transaction. Funds will appear in your wallet shortly.",
    )
    db.commit()
    return {"message": "Refund created and queued for processing.", "refund_id": str(refund.id)}


# ══════════════════════════════════════════════════════════════════
#  USER MANAGEMENT
# ══════════════════════════════════════════════════════════════════

@router.get("/users", summary="List all users")
def list_users(
    page: int = 1,
    per_page: int = 50,
    role: Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    q = db.query(User)
    if role:
        q = q.filter(User.role == role)
    total = q.count()
    users = q.offset((page - 1) * per_page).limit(per_page).all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "users": [
            {
                "id": str(u.id),
                "name": f"{u.first_name} {u.last_name}".strip(),
                "email": u.email,
                "role": u.role,
                "is_active": u.is_active,
                "is_admin": u.is_admin,
                "is_agent": u.is_agent,
                "is_kyc_verified": u.is_kyc_verified,
                "wallet_frozen": bool(u.wallet_frozen),
                "withdrawals_blocked": bool(u.withdrawals_blocked),
                "country_code": u.country_code,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
    }


@router.put("/users/{user_id}/deactivate", summary="Deactivate a user account")
def deactivate_user(
    user_id: str,
    reason: str = Body(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    if str(admin.id) == user_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "CANNOT_DEACTIVATE_SELF", "message": "You cannot deactivate your own admin account."},
        )
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "User not found."})

    user.is_active = False
    _notify(db, user_id, "Account Deactivated", f"Your account has been deactivated. Reason: {reason}. Contact support@milevault.com.")
    db.commit()
    return {"message": f"User '{user.email}' deactivated."}


@router.put("/users/{user_id}/freeze-wallet", summary="Freeze a user's wallet (block all wallet operations)")
def freeze_wallet(
    user_id: str,
    reason: str = Body(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "User not found."})
    user.wallet_frozen = True
    _notify(db, user_id, "Wallet Frozen", f"Your wallet has been frozen by platform security. Reason: {reason}. Contact support@milevault.com.")
    db.commit()
    return {"message": f"Wallet frozen for {user.email}."}


@router.put("/users/{user_id}/unfreeze-wallet", summary="Unfreeze a user's wallet")
def unfreeze_wallet(
    user_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "User not found."})
    user.wallet_frozen = False
    _notify(db, user_id, "Wallet Unfrozen", "Your wallet has been unfrozen. You can now perform wallet operations.")
    db.commit()
    return {"message": f"Wallet unfrozen for {user.email}."}


@router.put("/users/{user_id}/block-withdrawals", summary="Block withdrawals for a user")
def block_withdrawals(
    user_id: str,
    reason: str = Body(...),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "User not found."})
    user.withdrawals_blocked = True
    _notify(db, user_id, "Withdrawals Blocked", f"Withdrawals on your account have been blocked. Reason: {reason}.")
    db.commit()
    return {"message": f"Withdrawals blocked for {user.email}."}


@router.put("/users/{user_id}/unblock-withdrawals", summary="Unblock withdrawals for a user")
def unblock_withdrawals(
    user_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "User not found."})
    user.withdrawals_blocked = False
    _notify(db, user_id, "Withdrawals Unblocked", "Your withdrawal access has been restored.")
    db.commit()
    return {"message": f"Withdrawals unblocked for {user.email}."}


@router.post("/transfers/{wallet_txn_id}/reverse", summary="Reverse an internal wallet transfer")
def reverse_transfer(
    wallet_txn_id: str,
    reason: str = Body(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Admin reverses a transfer_out/transfer_in pair. Restores balances on both sides."""
    out_txn = db.query(WalletTransaction).filter(
        WalletTransaction.id == wallet_txn_id,
        WalletTransaction.type == "transfer_out",
        WalletTransaction.status == "completed",
    ).first()
    if not out_txn:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Completed transfer_out not found."})

    # Find matching transfer_in by reference
    in_txn = db.query(WalletTransaction).filter(
        WalletTransaction.reference == out_txn.reference,
        WalletTransaction.type == "transfer_in",
        WalletTransaction.status == "completed",
    ).first()

    # Reverse sender (add back)
    sender_bal = db.query(WalletBalance).filter(
        WalletBalance.user_id == out_txn.user_id,
        WalletBalance.currency == out_txn.currency,
    ).first()
    if sender_bal:
        sender_bal.amount = round(sender_bal.amount + out_txn.amount, 8)

    # Reverse recipient (deduct)
    if in_txn:
        recip_bal = db.query(WalletBalance).filter(
            WalletBalance.user_id == in_txn.user_id,
            WalletBalance.currency == in_txn.currency,
        ).first()
        if recip_bal:
            recip_bal.amount = max(0, round(recip_bal.amount - in_txn.amount, 8))
        in_txn.status = "reversed"
        _notify(db, in_txn.user_id, "Transfer Reversed", f"A transfer of {in_txn.currency} {in_txn.amount:.2f} to your wallet has been reversed by admin. Reason: {reason}")

    out_txn.status = "reversed"
    _notify(db, out_txn.user_id, "Transfer Reversed", f"Your transfer of {out_txn.currency} {out_txn.amount:.2f} has been reversed. Funds returned. Reason: {reason}")

    db.commit()
    return {"message": "Transfer reversed successfully.", "amount": out_txn.amount}


@router.put("/users/{user_id}/make-admin", summary="Grant admin rights to a user")
def make_admin(
    user_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "User not found."})
    user.is_admin = True
    db.commit()
    return {"message": f"Admin rights granted to '{user.email}'."}


# ══════════════════════════════════════════════════════════════════
#  TRANSACTION OVERSIGHT
# ══════════════════════════════════════════════════════════════════

@router.get("/transactions", summary="View all transactions")
def list_all_transactions(
    page: int = 1,
    per_page: int = 50,
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    q = db.query(Transaction)
    if status_filter:
        q = q.filter(Transaction.status == status_filter)
    total = q.count()
    txns = q.order_by(Transaction.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "transactions": [
            {
                "id": t.id,
                "title": t.title,
                "amount": t.amount,
                "currency": t.currency,
                "status": t.status,
                "type": t.type,
                "buyer": f"{t.buyer.first_name} {t.buyer.last_name}".strip() if t.buyer else None,
                "seller": f"{t.seller.first_name} {t.seller.last_name}".strip() if t.seller else None,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in txns
        ],
    }


@router.put("/transactions/{transaction_id}/force-status", summary="Admin force-changes a transaction status")
def force_status(
    transaction_id: str,
    new_status: str = Body(...),
    reason: str = Body(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    valid_statuses = {"pending_approval", "approved", "in_progress", "completed", "cancelled", "disputed"}
    if new_status not in valid_statuses:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_STATUS", "message": f"Status must be one of: {valid_statuses}"},
        )
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Transaction not found."})

    old_status = tx.status
    tx.status = new_status

    for uid in [str(tx.buyer_id), str(tx.seller_id) if tx.seller_id else None]:
        if uid:
            _notify(db, uid, "Transaction Status Changed",
                    f"Admin has updated your transaction '{tx.title}' from '{old_status}' to '{new_status}'. Reason: {reason}")
    db.commit()
    return {"message": f"Transaction status changed from '{old_status}' to '{new_status}'."}


# ══════════════════════════════════════════════════════════════════
#  DISPUTE OVERSIGHT  (admin sees EVERYTHING — no redaction)
# ══════════════════════════════════════════════════════════════════

def _user_full(u) -> dict:
    if not u:
        return None
    return {
        "id": str(u.id),
        "name": f"{u.first_name} {u.last_name}".strip(),
        "email": u.email,
        "role": u.role,
        "country_code": u.country_code,
        "is_kyc_verified": u.is_kyc_verified,
        "phone": getattr(u, "phone", None),
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.get("/disputes", summary="List all disputes (admin)")
def admin_list_disputes(
    status_filter: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    q = db.query(Dispute)
    if status_filter:
        q = q.filter(Dispute.status == status_filter)
    total = q.count()
    disputes = q.order_by(Dispute.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()

    result = []
    for d in disputes:
        tx = d.transaction
        result.append({
            "id": str(d.id),
            "title": d.title,
            "reason": d.reason,
            "status": d.status,
            "raised_by": _user_full(d.raised_by_user),
            "transaction": {
                "id": str(tx.id) if tx else None,
                "title": tx.title if tx else None,
                "amount": tx.amount if tx else None,
                "currency": tx.currency if tx else None,
                "status": tx.status if tx else None,
            } if tx else None,
            "documents_count": len(d.documents),
            "created_at": d.created_at.isoformat() if d.created_at else None,
        })
    return {"total": total, "page": page, "per_page": per_page, "disputes": result}


@router.get("/disputes/{dispute_id}", summary="Full dispute detail — all info visible to admin")
def admin_get_dispute(
    dispute_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """
    Admin endpoint: returns the full dispute package —
    contract details, all chat messages, both parties' profiles,
    all evidence documents, milestones, and agent info.
    No seller or buyer information is redacted for admin.
    """
    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Dispute not found."})

    tx = dispute.transaction
    if not tx:
        raise HTTPException(status_code=404, detail={"error": "TRANSACTION_MISSING", "message": "Associated transaction not found."})

    buyer = db.query(User).filter(User.id == tx.buyer_id).first()
    seller = db.query(User).filter(User.id == tx.seller_id).first() if tx.seller_id else None

    # All chat messages for this transaction
    chat = db.query(ChatMessage).filter(
        ChatMessage.transaction_id == tx.id
    ).order_by(ChatMessage.created_at.asc()).all()

    # Milestones
    milestones = [
        {
            "id": str(m.id),
            "title": m.title,
            "amount": m.amount,
            "status": m.status,
            "due_date": m.due_date.isoformat() if m.due_date else None,
            "completed_date": m.completed_date.isoformat() if m.completed_date else None,
            "description": m.description,
            "feedback": m.feedback,
        }
        for m in (tx.milestones or [])
    ]

    # Agent info (if requested)
    agent_info = None
    agent_thread_messages = []
    if tx.agent_request:
        req = tx.agent_request
        agent_info = {
            "request_id": str(req.id),
            "status": req.status,
            "agent": {
                "id": str(req.agent.id) if req.agent else None,
                "name": f"{req.agent.user.first_name} {req.agent.user.last_name}".strip() if req.agent and req.agent.user else None,
                "specialty": req.agent.specialty if req.agent else None,
            } if req.agent else None,
            "evidence_s3_keys": req.evidence_s3_keys or [],
        }
        msgs = (
            db.query(AgentRequestMessage)
            .filter(AgentRequestMessage.agent_request_id == req.id)
            .order_by(AgentRequestMessage.created_at.asc())
            .all()
        )
        agent_thread_messages = [
            {
                "id": str(m.id),
                "author_user_id": str(m.author_user_id),
                "author_role": m.author_role,
                "body": m.body,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in msgs
        ]

    return {
        "dispute": {
            "id": str(dispute.id),
            "title": dispute.title,
            "description": dispute.description,
            "reason": dispute.reason,
            "suggested_resolution": dispute.suggested_resolution,
            "status": dispute.status,
            "resolution": dispute.resolution,
            "created_at": dispute.created_at.isoformat() if dispute.created_at else None,
            "updated_at": dispute.updated_at.isoformat() if dispute.updated_at else None,
        },
        "contract": {
            "id": str(tx.id),
            "title": tx.title,
            "description": tx.description,
            "amount": tx.amount,
            "currency": tx.currency,
            "type": tx.type,
            "status": tx.status,
            "service_fee_payment": tx.service_fee_payment,
            "buyer_fee_ratio": tx.buyer_fee_ratio,
            "seller_fee_ratio": tx.seller_fee_ratio,
            "notes": tx.notes,
            "supporting_url": tx.supporting_url,
            "project_url": tx.project_url,
            "contract_signed": tx.contract_signed,
            "terms_accepted": tx.terms_accepted,
            "expected_completion_date": tx.expected_completion_date.isoformat() if tx.expected_completion_date else None,
            "created_at": tx.created_at.isoformat() if tx.created_at else None,
            "milestones": milestones,
        },
        "buyer": _user_full(buyer),
        "seller": _user_full(seller),
        "chat_messages": [
            {
                "id": str(m.id),
                "sender_id": str(m.sender_id),
                "sender_name": f"{m.sender.first_name} {m.sender.last_name}".strip() if m.sender else "Unknown",
                "sender_role": m.sender.role if m.sender else None,
                "message": m.message,
                "attachments": m.attachments or [],
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in chat
        ],
        "evidence_documents": [
            {
                "id": str(doc.id),
                "file_url": doc.file_url,
                "file_name": doc.file_name,
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
            }
            for doc in dispute.documents
        ],
        "agent": agent_info,
        "agent_thread_messages": agent_thread_messages,
    }


@router.put("/disputes/{dispute_id}", summary="Admin resolves or updates a dispute")
def admin_update_dispute(
    dispute_id: str,
    new_status: str = Body(...),
    resolution: Optional[str] = Body(None),
    agent_fee_action: Optional[str] = Body(
        None,
        description="On resolve: refund_buyer | release_agent | omit for default (pay only if agent completed verification)",
    ),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    valid = {"open", "in_review", "resolved", "closed"}
    if new_status not in valid:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_STATUS", "message": f"Status must be one of: {valid}"},
        )
    if agent_fee_action is not None and agent_fee_action not in ("refund_buyer", "release_agent"):
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_AGENT_FEE_ACTION", "message": "agent_fee_action must be refund_buyer, release_agent, or null."},
        )

    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Dispute not found."})

    dispute.status = new_status
    if resolution:
        dispute.resolution = resolution

    fee_out = None
    tx = dispute.transaction
    if new_status in ("resolved", "closed") and tx:
        tx.status = "completed" if new_status == "resolved" else tx.status
        for uid in filter(None, [str(tx.buyer_id), str(tx.seller_id) if tx.seller_id else None]):
            _notify(
                db, uid,
                "Dispute Resolved" if new_status == "resolved" else "Dispute Closed",
                f"Your dispute '{dispute.title}' has been {new_status} by an admin."
                + (f" Resolution: {resolution}" if resolution else ""),
            )
        if new_status == "resolved":
            from app.services.agent_fee_service import apply_agent_fee_policy_after_dispute

            fee_out = apply_agent_fee_policy_after_dispute(db, tx, agent_fee_action)

    db.commit()
    return {
        "message": f"Dispute updated to '{new_status}'.",
        "dispute_id": dispute_id,
        "agent_fee_policy": fee_out,
    }


# ══════════════════════════════════════════════════════════════════
#  PLATFORM SETTINGS  (escrow fee %, withdrawal fee, limits)
# ══════════════════════════════════════════════════════════════════

def _get_settings(db: Session) -> PlatformSettings:
    """Get or create the singleton platform settings row."""
    s = db.query(PlatformSettings).filter(PlatformSettings.id == "default").first()
    if not s:
        s = PlatformSettings(id="default")
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def _settings_to_dict(s: PlatformSettings) -> dict:
    return {
        "escrow_fee_percent": s.escrow_fee_percent,
        "min_fee_amount": s.min_fee_amount,
        "max_fee_amount": s.max_fee_amount,
        "fee_currency": s.fee_currency,
        "fee_paid_by": s.fee_paid_by,
        "buyer_fee_share": s.buyer_fee_share,
        "seller_fee_share": s.seller_fee_share,
        "withdrawal_fee_percent": s.withdrawal_fee_percent,
        "withdrawal_fee_fixed": s.withdrawal_fee_fixed,
        "min_transaction_amount": s.min_transaction_amount,
        "max_transaction_amount": s.max_transaction_amount,
        "high_value_checklist_threshold": getattr(s, "high_value_checklist_threshold", None),
        "funding_deadline_days": getattr(s, "funding_deadline_days", 14),
        "auto_release_days": getattr(s, "auto_release_days", 5),
        "invite_expiry_days": getattr(s, "invite_expiry_days", 30),
        "stale_activity_warn_days": getattr(s, "stale_activity_warn_days", 90),
        "require_email_verification": bool(getattr(s, "require_email_verification", True)),
        "platform_name": s.platform_name,
        "support_email": s.support_email,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/settings", summary="Get platform fee and policy settings")
def get_platform_settings(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    return _settings_to_dict(_get_settings(db))


@router.put("/settings", summary="Update platform fee and policy settings")
def update_platform_settings(
    escrow_fee_percent: Optional[float] = Body(None),
    min_fee_amount: Optional[float] = Body(None),
    max_fee_amount: Optional[float] = Body(None),
    fee_currency: Optional[str] = Body(None),
    fee_paid_by: Optional[str] = Body(None),
    buyer_fee_share: Optional[float] = Body(None),
    seller_fee_share: Optional[float] = Body(None),
    withdrawal_fee_percent: Optional[float] = Body(None),
    withdrawal_fee_fixed: Optional[float] = Body(None),
    min_transaction_amount: Optional[float] = Body(None),
    max_transaction_amount: Optional[float] = Body(None),
    high_value_checklist_threshold: Optional[float] = Body(None),
    funding_deadline_days: Optional[int] = Body(None),
    auto_release_days: Optional[int] = Body(None),
    invite_expiry_days: Optional[int] = Body(None),
    stale_activity_warn_days: Optional[int] = Body(None),
    require_email_verification: Optional[bool] = Body(None),
    platform_name: Optional[str] = Body(None),
    support_email: Optional[str] = Body(None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Update any subset of platform settings. All fields are optional."""
    if escrow_fee_percent is not None and not (0 <= escrow_fee_percent <= 50):
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_FEE", "message": "Escrow fee must be between 0% and 50%."},
        )
    if fee_paid_by is not None and fee_paid_by not in ("buyer", "seller", "split"):
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_FEE_PAYER", "message": "fee_paid_by must be 'buyer', 'seller', or 'split'."},
        )
    if buyer_fee_share is not None and seller_fee_share is not None:
        if abs((buyer_fee_share + seller_fee_share) - 100.0) > 0.01:
            raise HTTPException(
                status_code=422,
                detail={"error": "FEE_SHARE_MISMATCH", "message": "buyer_fee_share + seller_fee_share must equal 100."},
            )

    if funding_deadline_days is not None and not (1 <= int(funding_deadline_days) <= 366):
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_FUNDING_DEADLINE", "message": "funding_deadline_days must be between 1 and 366."},
        )
    if auto_release_days is not None and not (3 <= int(auto_release_days) <= 7):
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_AUTO_RELEASE", "message": "auto_release_days must be between 3 and 7."},
        )
    if invite_expiry_days is not None and not (7 <= int(invite_expiry_days) <= 180):
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_INVITE_EXPIRY", "message": "invite_expiry_days must be between 7 and 180."},
        )
    if stale_activity_warn_days is not None and not (30 <= int(stale_activity_warn_days) <= 730):
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_STALE_WARN", "message": "stale_activity_warn_days must be between 30 and 730."},
        )

    s = _get_settings(db)
    fields = {
        "escrow_fee_percent": escrow_fee_percent,
        "min_fee_amount": min_fee_amount,
        "max_fee_amount": max_fee_amount,
        "fee_currency": fee_currency,
        "fee_paid_by": fee_paid_by,
        "buyer_fee_share": buyer_fee_share,
        "seller_fee_share": seller_fee_share,
        "withdrawal_fee_percent": withdrawal_fee_percent,
        "withdrawal_fee_fixed": withdrawal_fee_fixed,
        "min_transaction_amount": min_transaction_amount,
        "max_transaction_amount": max_transaction_amount,
        "high_value_checklist_threshold": high_value_checklist_threshold,
        "funding_deadline_days": funding_deadline_days,
        "auto_release_days": auto_release_days,
        "invite_expiry_days": invite_expiry_days,
        "stale_activity_warn_days": stale_activity_warn_days,
        "require_email_verification": require_email_verification,
        "platform_name": platform_name,
        "support_email": support_email,
    }
    for k, v in fields.items():
        if v is not None:
            setattr(s, k, v)
    s.updated_by = admin.id
    db.commit()
    db.refresh(s)
    return {"message": "Platform settings updated.", "settings": _settings_to_dict(s)}


@router.get("/settings/fee-preview", summary="Preview the fee for a given deal amount")
def fee_preview(
    amount: float,
    currency: str = "USD",
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    """Calculate what the escrow fee would be for a given deal amount."""
    s = _get_settings(db)
    raw_fee = round(amount * s.escrow_fee_percent / 100, 8)
    if s.min_fee_amount:
        raw_fee = max(raw_fee, s.min_fee_amount)
    if s.max_fee_amount:
        raw_fee = min(raw_fee, s.max_fee_amount)

    buyer_pays = round(raw_fee * s.buyer_fee_share / 100, 8)
    seller_pays = round(raw_fee * s.seller_fee_share / 100, 8)

    return {
        "deal_amount": amount,
        "currency": currency,
        "fee_percent": s.escrow_fee_percent,
        "total_fee": raw_fee,
        "buyer_pays": buyer_pays,
        "seller_pays": seller_pays,
        "fee_paid_by": s.fee_paid_by,
    }


# ══════════════════════════════════════════════════════════════════
#  WITHDRAWAL APPROVAL
# ══════════════════════════════════════════════════════════════════

@router.get("/withdrawals", summary="List withdrawal requests")
def list_withdrawals(
    status: Optional[str] = "pending",
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    q = db.query(WalletTransaction).filter(WalletTransaction.type == "withdrawal")
    if status and status != "all":
        q = q.filter(WalletTransaction.status == status)
    txns = q.order_by(WalletTransaction.created_at.desc()).limit(200).all()
    result = []
    for t in txns:
        u = db.query(User).filter(User.id == t.user_id).first()
        result.append({
            "id": str(t.id),
            "user_id": str(t.user_id),
            "user_name": f"{u.first_name} {u.last_name}".strip() if u else "Unknown",
            "user_email": u.email if u else "Unknown",
            "amount": t.amount,
            "currency": t.currency,
            "status": t.status,
            "method": t.method,
            "description": t.description,
            "reference": t.reference,
            "flagged_high_risk": bool(getattr(t, "flagged_high_risk", False)),
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })
    return {"withdrawals": result, "total": len(result)}


@router.put("/withdrawals/{txn_id}/approve", summary="Approve a withdrawal — admin marks it processed")
def approve_withdrawal(
    txn_id: str,
    notes: Optional[str] = Body(None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    txn = db.query(WalletTransaction).filter(
        WalletTransaction.id == txn_id,
        WalletTransaction.type == "withdrawal",
    ).first()
    if not txn:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Withdrawal not found."})
    if txn.status != "pending":
        raise HTTPException(
            status_code=409,
            detail={"error": "INVALID_STATUS", "message": f"Withdrawal is already '{txn.status}'."},
        )

    txn.status = "completed"
    if notes:
        txn.reference = notes

    # Release from pending_amount
    balance = db.query(WalletBalance).filter(
        WalletBalance.user_id == txn.user_id,
        WalletBalance.currency == txn.currency,
    ).first()
    if balance and balance.pending_amount >= txn.amount:
        balance.pending_amount = round(balance.pending_amount - txn.amount, 8)

    _notify(
        db, txn.user_id,
        "Withdrawal Approved",
        f"Your withdrawal of {txn.currency} {txn.amount:.2f} has been approved and processed.{' Notes: ' + notes if notes else ''}",
    )
    db.commit()
    return {"message": "Withdrawal approved and marked as completed.", "id": txn_id}


@router.put("/withdrawals/{txn_id}/reject", summary="Reject a withdrawal — funds returned to user balance")
def reject_withdrawal(
    txn_id: str,
    reason: str = Body(...),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    txn = db.query(WalletTransaction).filter(
        WalletTransaction.id == txn_id,
        WalletTransaction.type == "withdrawal",
    ).first()
    if not txn:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Withdrawal not found."})
    if txn.status != "pending":
        raise HTTPException(
            status_code=409,
            detail={"error": "INVALID_STATUS", "message": f"Withdrawal is already '{txn.status}'."},
        )

    # Return funds
    balance = db.query(WalletBalance).filter(
        WalletBalance.user_id == txn.user_id,
        WalletBalance.currency == txn.currency,
    ).first()
    if balance:
        balance.amount = round(balance.amount + txn.amount, 8)
        if balance.pending_amount >= txn.amount:
            balance.pending_amount = round(balance.pending_amount - txn.amount, 8)

    txn.status = "failed"
    txn.description = f"Rejected by admin: {reason}"

    _notify(
        db, txn.user_id,
        "Withdrawal Rejected",
        f"Your withdrawal of {txn.currency} {txn.amount:.2f} was rejected. Reason: {reason}. Funds have been returned to your balance.",
    )
    db.commit()
    return {"message": "Withdrawal rejected. Funds returned to user balance.", "id": txn_id}


# ══════════════════════════════════════════════════════════════════
#  AGENT SUBSCRIPTION PLANS  (admin manages available plans)
# ══════════════════════════════════════════════════════════════════

@router.get("/agent-plans", summary="List all agent subscription plans")
def list_agent_plans(db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    plans = db.query(AgentSubscriptionPlan).order_by(AgentSubscriptionPlan.price).all()
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "display_name": p.display_name,
            "price": p.price,
            "currency": p.currency,
            "duration_months": p.duration_months,
            "priority_boost": p.priority_boost,
            "features": p.features or [],
            "is_active": p.is_active,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in plans
    ]


@router.post("/agent-plans", status_code=201, summary="Create an agent subscription plan")
def create_agent_plan(
    name: str = Body(...),
    display_name: str = Body(...),
    price: float = Body(...),
    currency: str = Body("USD"),
    duration_months: int = Body(1),
    priority_boost: int = Body(0),
    features: list = Body(default=[]),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    if db.query(AgentSubscriptionPlan).filter(AgentSubscriptionPlan.name == name).first():
        raise HTTPException(status_code=409, detail={"error": "PLAN_EXISTS", "message": f"Plan '{name}' already exists."})
    plan = AgentSubscriptionPlan(
        name=name.lower().strip(),
        display_name=display_name.strip(),
        price=price,
        currency=currency.upper(),
        duration_months=duration_months,
        priority_boost=priority_boost,
        features=features,
        is_active=True,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return {"message": f"Plan '{display_name}' created.", "id": str(plan.id)}


@router.put("/agent-plans/{plan_id}", summary="Update an agent subscription plan")
def update_agent_plan(
    plan_id: str,
    display_name: Optional[str] = Body(None),
    price: Optional[float] = Body(None),
    priority_boost: Optional[int] = Body(None),
    features: Optional[list] = Body(None),
    is_active: Optional[bool] = Body(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    plan = db.query(AgentSubscriptionPlan).filter(AgentSubscriptionPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Plan not found."})
    if display_name is not None:
        plan.display_name = display_name.strip()
    if price is not None:
        plan.price = price
    if priority_boost is not None:
        plan.priority_boost = priority_boost
    if features is not None:
        plan.features = features
    if is_active is not None:
        plan.is_active = is_active
    db.commit()
    return {"message": "Plan updated."}


@router.delete("/agent-plans/{plan_id}", summary="Deactivate an agent subscription plan")
def delete_agent_plan(
    plan_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    plan = db.query(AgentSubscriptionPlan).filter(AgentSubscriptionPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Plan not found."})
    plan.is_active = False
    db.commit()
    return {"message": "Plan deactivated."}


# ══════════════════════════════════════════════════════════════════
#  AGENT SERVICE TIERS  (fee by transaction value range)
# ══════════════════════════════════════════════════════════════════

@router.get("/agent-service-tiers", summary="List agent service fee tiers")
def list_agent_service_tiers(db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    tiers = db.query(AgentServiceTier).order_by(AgentServiceTier.min_transaction_amount).all()
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "min_transaction_amount": t.min_transaction_amount,
            "max_transaction_amount": t.max_transaction_amount,
            "fee_type": t.fee_type,
            "fee_amount": t.fee_amount,
            "agent_payout_percent": t.agent_payout_percent,
            "currency": t.currency,
            "description": t.description,
            "is_active": t.is_active,
        }
        for t in tiers
    ]


@router.post("/agent-service-tiers", status_code=201, summary="Create an agent service fee tier")
def create_agent_service_tier(
    name: str = Body(...),
    min_transaction_amount: float = Body(...),
    max_transaction_amount: Optional[float] = Body(None),
    fee_type: str = Body("flat"),
    fee_amount: float = Body(...),
    agent_payout_percent: float = Body(70.0),
    currency: str = Body("USD"),
    description: Optional[str] = Body(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    if fee_type not in ("flat", "percent"):
        raise HTTPException(status_code=422, detail={"error": "INVALID_FEE_TYPE", "message": "fee_type must be 'flat' or 'percent'."})
    if not (0 < agent_payout_percent <= 100):
        raise HTTPException(status_code=422, detail={"error": "INVALID_PAYOUT", "message": "agent_payout_percent must be between 1 and 100."})
    tier = AgentServiceTier(
        name=name.strip(),
        min_transaction_amount=min_transaction_amount,
        max_transaction_amount=max_transaction_amount,
        fee_type=fee_type,
        fee_amount=fee_amount,
        agent_payout_percent=agent_payout_percent,
        currency=currency.upper(),
        description=description,
        is_active=True,
    )
    db.add(tier)
    db.commit()
    db.refresh(tier)
    return {"message": f"Service tier '{name}' created.", "id": str(tier.id)}


@router.put("/agent-service-tiers/{tier_id}", summary="Update an agent service fee tier")
def update_agent_service_tier(
    tier_id: str,
    name: Optional[str] = Body(None),
    min_transaction_amount: Optional[float] = Body(None),
    max_transaction_amount: Optional[float] = Body(None),
    fee_type: Optional[str] = Body(None),
    fee_amount: Optional[float] = Body(None),
    agent_payout_percent: Optional[float] = Body(None),
    is_active: Optional[bool] = Body(None),
    description: Optional[str] = Body(None),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    tier = db.query(AgentServiceTier).filter(AgentServiceTier.id == tier_id).first()
    if not tier:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Service tier not found."})
    for field, val in {
        "name": name, "min_transaction_amount": min_transaction_amount,
        "max_transaction_amount": max_transaction_amount, "fee_type": fee_type,
        "fee_amount": fee_amount, "agent_payout_percent": agent_payout_percent,
        "is_active": is_active, "description": description,
    }.items():
        if val is not None:
            setattr(tier, field, val)
    db.commit()
    return {"message": "Service tier updated."}


@router.delete("/agent-service-tiers/{tier_id}", summary="Deactivate an agent service fee tier")
def delete_agent_service_tier(
    tier_id: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    tier = db.query(AgentServiceTier).filter(AgentServiceTier.id == tier_id).first()
    if not tier:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Service tier not found."})
    tier.is_active = False
    db.commit()
    return {"message": "Service tier deactivated."}


# ══════════════════════════════════════════════════════════════════
#  AGENT REQUEST MANAGEMENT  (admin assigns agents, views all)
# ══════════════════════════════════════════════════════════════════

@router.get("/agent-requests", summary="List all agent requests platform-wide")
def list_all_agent_requests(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    q = db.query(AgentRequest)
    if status_filter:
        q = q.filter(AgentRequest.status == status_filter)
    reqs = q.order_by(AgentRequest.created_at.desc()).limit(200).all()
    result = []
    for r in reqs:
        tx = db.query(Transaction).filter(Transaction.id == str(r.transaction_id)).first()
        buyer = db.query(User).filter(User.id == str(r.buyer_id)).first()
        result.append({
            "id": str(r.id),
            "transaction_id": str(r.transaction_id),
            "transaction_title": tx.title if tx else None,
            "transaction_amount": tx.amount if tx else None,
            "buyer_name": f"{buyer.first_name} {buyer.last_name}".strip() if buyer else None,
            "buyer_email": buyer.email if buyer else None,
            "agent_id": str(r.agent_id) if r.agent_id else None,
            "agent_name": (
                f"{r.agent.user.first_name} {r.agent.user.last_name}".strip()
                if r.agent and r.agent.user else None
            ),
            "status": r.status,
            "fee_charged": r.fee_charged,
            "fee_currency": r.fee_currency,
            "agent_payout_amount": r.agent_payout_amount,
            "payout_status": r.payout_status,
            "assigned_by_admin": r.assigned_by_admin,
            "buyer_message": r.buyer_message,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        })
    return {"requests": result, "total": len(result)}


@router.post("/agent-requests/{request_id}/assign", summary="Admin assigns an agent to a request")
def admin_assign_agent(
    request_id: str,
    agent_id: str = Body(...),
    notes: Optional[str] = Body(None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """
    Admin can assign (or reassign) any approved agent to a buyer's agent request.
    This overrides the buyer's original agent choice or fills an unassigned request.
    """
    req = db.query(AgentRequest).filter(AgentRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Agent request not found."})
    if req.status == AgentRequestStatus.completed:
        raise HTTPException(status_code=409, detail={"error": "ALREADY_COMPLETED", "message": "Cannot reassign a completed request."})

    new_agent = db.query(Agent).filter(
        Agent.id == agent_id,
        Agent.status == AgentStatus.approved,
    ).first()
    if not new_agent:
        raise HTTPException(status_code=404, detail={"error": "AGENT_NOT_FOUND", "message": "Approved agent not found."})

    req.agent_id = new_agent.id
    req.assigned_by_admin = True
    req.assigned_by = str(admin.id)
    req.status = AgentRequestStatus.active

    # Recalculate fee if not already set
    if not req.fee_charged:
        tx = db.query(Transaction).filter(Transaction.id == str(req.transaction_id)).first()
        if tx:
            from app.models.agent import AgentServiceTier
            tiers = db.query(AgentServiceTier).filter(AgentServiceTier.is_active == True).all()
            for tier in tiers:
                if tier.min_transaction_amount <= tx.amount:
                    if tier.max_transaction_amount is None or tx.amount <= tier.max_transaction_amount:
                        if tier.fee_type == "flat":
                            req.fee_charged = tier.fee_amount
                        else:
                            req.fee_charged = round(tx.amount * tier.fee_amount / 100, 2)
                        req.fee_currency = tier.currency
                        req.agent_payout_amount = round(req.fee_charged * tier.agent_payout_percent / 100, 2)
                        break

    # Create earnings record
    if req.fee_charged and req.agent_payout_amount:
        existing_earning = db.query(AgentEarning).filter(AgentEarning.request_id == req.id).first()
        if not existing_earning:
            db.add(AgentEarning(
                agent_id=new_agent.id,
                request_id=req.id,
                gross_fee=req.fee_charged,
                agent_payout=req.agent_payout_amount,
                platform_cut=req.fee_charged - req.agent_payout_amount,
                currency=req.fee_currency or "USD",
                status="pending",
            ))

    _notify(db, str(new_agent.user_id), "You've Been Assigned to a Project",
            f"An admin has assigned you to a verification request. Check your agent dashboard.")
    _notify(db, str(req.buyer_id), "Agent Assigned to Your Request",
            f"An admin has assigned a verified agent to your request." + (f" Notes: {notes}" if notes else ""))
    db.commit()
    return {"message": f"Agent assigned and request is now active.", "request_id": request_id}


# ══════════════════════════════════════════════════════════════════
#  AGENT EARNINGS & PAYOUTS
# ══════════════════════════════════════════════════════════════════

@router.get("/agent-earnings", summary="List all agent earnings/payout records")
def list_agent_earnings(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    q = db.query(AgentEarning)
    if status_filter:
        q = q.filter(AgentEarning.status == status_filter)
    earnings = q.order_by(AgentEarning.created_at.desc()).limit(200).all()
    result = []
    for e in earnings:
        agent_user = e.agent.user if e.agent else None
        result.append({
            "id": str(e.id),
            "agent_id": str(e.agent_id),
            "agent_name": f"{agent_user.first_name} {agent_user.last_name}".strip() if agent_user else None,
            "agent_email": agent_user.email if agent_user else None,
            "request_id": str(e.request_id) if e.request_id else None,
            "gross_fee": e.gross_fee,
            "agent_payout": e.agent_payout,
            "platform_cut": e.platform_cut,
            "currency": e.currency,
            "status": e.status,
            "admin_payout_approved": bool(getattr(e, "admin_payout_approved", False)),
            "paid_at": e.paid_at.isoformat() if e.paid_at else None,
            "notes": e.notes,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })
    return {"earnings": result, "total": len(result)}


@router.post("/agent-earnings/{earning_id}/payout", summary="Admin approves payout (ledger) for held earnings, or legacy mark-paid")
def process_agent_payout(
    earning_id: str,
    notes: Optional[str] = Body(None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    earning = db.query(AgentEarning).filter(AgentEarning.id == earning_id).first()
    if not earning:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Earning record not found."})
    if earning.status == "paid":
        raise HTTPException(status_code=409, detail={"error": "ALREADY_PAID", "message": "This earning has already been paid out."})

    if earning.status == "held":
        from app.services.agent_fee_service import admin_force_payout_held_earning

        admin_force_payout_held_earning(db, earning, str(admin.id))
        if notes:
            earning.notes = notes
        agent_user = earning.agent.user if earning.agent else None
        if agent_user:
            _notify(
                db,
                str(agent_user.id),
                "Payout Processed",
                f"Your payout of {earning.currency} {earning.agent_payout:.2f} has been released from escrow to your wallet."
                + (f" Notes: {notes}" if notes else ""),
            )
        db.commit()
        return {"message": f"Payout of {earning.currency} {earning.agent_payout:.2f} released.", "earning_id": earning_id}

    # Legacy rows (e.g. admin-assigned without ledger hold): bookkeeping-only mark paid
    earning.status = "paid"
    earning.paid_at = datetime.utcnow()
    earning.notes = notes
    if earning.agent:
        earning.agent.total_earnings = (earning.agent.total_earnings or 0) + earning.agent_payout
    agent_user = earning.agent.user if earning.agent else None
    if agent_user:
        _notify(
            db,
            str(agent_user.id),
            "Payout Processed",
            f"Your payout of {earning.currency} {earning.agent_payout:.2f} has been processed (legacy record)."
            + (f" Notes: {notes}" if notes else ""),
        )
    db.commit()
    return {"message": f"Payout of {earning.currency} {earning.agent_payout:.2f} marked as paid.", "earning_id": earning_id}


@router.get("/agent-subscriptions", summary="List all active agent subscriptions")
def list_agent_subscriptions(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    subs = db.query(AgentSubscription).filter(
        AgentSubscription.is_active == True
    ).order_by(AgentSubscription.expires_at.desc()).all()
    return [
        {
            "id": str(s.id),
            "agent_id": str(s.agent_id),
            "agent_name": (
                f"{s.agent.user.first_name} {s.agent.user.last_name}".strip()
                if s.agent and s.agent.user else None
            ),
            "plan_name": s.plan.display_name if s.plan else None,
            "plan_price": s.plan.price if s.plan else None,
            "currency": s.plan.currency if s.plan else None,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            "payment_reference": s.payment_reference,
        }
        for s in subs
    ]
