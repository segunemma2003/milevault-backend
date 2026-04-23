"""
Admin router — full platform control panel.
All endpoints require is_admin=True.
Covers: currencies, exchange rates, payment gateways, agent approval, refunds, transaction oversight.
"""
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Body
from sqlalchemy.orm import Session
from app.database import get_db
from app.dependencies import get_current_admin
from app.models.user import User
from app.models.currency import Currency, ExchangeRate, PaymentGateway, Refund, CurrencyType, PaymentGatewayName, PlatformSettings
from app.models.agent import Agent, AgentStatus
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

    refund = Refund(
        transaction_id=transaction_id,
        dispute_id=dispute_id,
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
    }


@router.put("/disputes/{dispute_id}", summary="Admin resolves or updates a dispute")
def admin_update_dispute(
    dispute_id: str,
    new_status: str = Body(...),
    resolution: Optional[str] = Body(None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    valid = {"open", "in_review", "resolved", "closed"}
    if new_status not in valid:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_STATUS", "message": f"Status must be one of: {valid}"},
        )

    dispute = db.query(Dispute).filter(Dispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Dispute not found."})

    dispute.status = new_status
    if resolution:
        dispute.resolution = resolution

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

    db.commit()
    return {"message": f"Dispute updated to '{new_status}'.", "dispute_id": dispute_id}


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
