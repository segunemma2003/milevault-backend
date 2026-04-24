"""
Agent router — registration, specialty management, buyer requests, subscriptions, earnings.
Rules enforced here:
  - Agents CANNOT communicate with sellers or see seller private details.
  - Buyers (or admin) request agents; agents accept/decline.
  - Agents upload evidence visible only to buyer + admin.
  - KYC verification is required before registering as an agent.
  - Subscription tier affects priority in the agent listing.
"""
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, status, UploadFile, Body
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.agent import (
    Agent, AgentRequest, AgentSpecialty, AgentStatus, AgentRequestStatus,
    AgentSubscriptionPlan, AgentSubscription, AgentServiceTier, AgentEarning,
)
from app.models.transaction import Transaction
from app.models.user import User
from app.models.notification import Notification
from app.dependencies import get_current_user, get_current_approved_agent
from app.services.s3_service import get_presigned_upload_url, save_local_upload
from app.services.cache_service import cache_set, cache_get

router = APIRouter(prefix="/agents", tags=["Agents"])


# ─── Helpers ────────────────────────────────────────────────────────────────

def _agent_to_dict(agent: Agent, include_user: bool = True) -> dict:
    d = {
        "id": str(agent.id),
        "user_id": str(agent.user_id),
        "specialty": agent.specialty,
        "specialty_details": agent.specialty_details,
        "certifications": agent.certifications or [],
        "years_experience": agent.years_experience,
        "hourly_rate": agent.hourly_rate,
        "hourly_rate_currency": agent.hourly_rate_currency,
        "portfolio_url": agent.portfolio_url,
        "status": agent.status,
        "is_available": agent.is_available,
        "total_verifications": agent.total_verifications,
        "rating": agent.rating,
        "rating_count": agent.rating_count,
        "subscription_priority": agent.subscription_priority,
        "total_earnings": agent.total_earnings,
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
    }
    if include_user and agent.user:
        d["name"] = f"{agent.user.first_name} {agent.user.last_name}".strip()
        d["avatar_url"] = agent.user.avatar_url
        d["email"] = agent.user.email
    return d


def _request_to_dict(req: AgentRequest) -> dict:
    return {
        "id": str(req.id),
        "transaction_id": str(req.transaction_id),
        "buyer_id": str(req.buyer_id),
        "agent_id": str(req.agent_id) if req.agent_id else None,
        "status": req.status,
        "buyer_message": req.buyer_message,
        "fee_charged": req.fee_charged,
        "fee_currency": req.fee_currency,
        "agent_payout_amount": req.agent_payout_amount,
        "payout_status": req.payout_status,
        "assigned_by_admin": req.assigned_by_admin,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "completed_at": req.completed_at.isoformat() if req.completed_at else None,
        "agent": _agent_to_dict(req.agent) if req.agent else None,
    }


def _notify(db: Session, user_id: str, title: str, message: str, ntype: str = "agent"):
    db.add(Notification(user_id=user_id, title=title, message=message, type=ntype))


def _calculate_agent_fee(db: Session, transaction_amount: float) -> Optional[AgentServiceTier]:
    """Find the applicable service tier for a transaction amount."""
    tiers = db.query(AgentServiceTier).filter(AgentServiceTier.is_active == True).all()
    for tier in tiers:
        if tier.min_transaction_amount <= transaction_amount:
            if tier.max_transaction_amount is None or transaction_amount <= tier.max_transaction_amount:
                return tier
    return None


# ─── Subscription plans (public read) ────────────────────────────────────────

@router.get("/subscription/plans", summary="List available agent subscription plans")
def list_subscription_plans(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    plans = db.query(AgentSubscriptionPlan).filter(
        AgentSubscriptionPlan.is_active == True
    ).order_by(AgentSubscriptionPlan.price).all()
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
        }
        for p in plans
    ]


@router.get("/subscription/status", summary="Get current agent's subscription status")
def get_subscription_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "AGENT_NOT_FOUND", "message": "No agent profile found."})

    active_sub = db.query(AgentSubscription).filter(
        AgentSubscription.agent_id == agent.id,
        AgentSubscription.is_active == True,
        AgentSubscription.expires_at > datetime.utcnow(),
    ).order_by(AgentSubscription.expires_at.desc()).first()

    return {
        "has_active_subscription": active_sub is not None,
        "subscription": {
            "id": str(active_sub.id),
            "plan": {
                "id": str(active_sub.plan.id),
                "name": active_sub.plan.name,
                "display_name": active_sub.plan.display_name,
                "price": active_sub.plan.price,
                "currency": active_sub.plan.currency,
                "priority_boost": active_sub.plan.priority_boost,
            },
            "started_at": active_sub.started_at.isoformat(),
            "expires_at": active_sub.expires_at.isoformat(),
        } if active_sub else None,
    }


@router.post("/subscribe", status_code=status.HTTP_201_CREATED, summary="Agent subscribes to a plan")
def subscribe_to_plan(
    plan_id: str = Body(...),
    payment_reference: Optional[str] = Body(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Subscribe to a monthly agent plan. In production this would verify a Paystack payment;
    for now a payment_reference is stored and admin can verify manually.
    """
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "AGENT_NOT_FOUND", "message": "Register as an agent first."})
    if agent.status != AgentStatus.approved:
        raise HTTPException(status_code=403, detail={"error": "NOT_APPROVED", "message": "Only approved agents can subscribe."})

    plan = db.query(AgentSubscriptionPlan).filter(
        AgentSubscriptionPlan.id == plan_id,
        AgentSubscriptionPlan.is_active == True,
    ).first()
    if not plan:
        raise HTTPException(status_code=404, detail={"error": "PLAN_NOT_FOUND", "message": "Subscription plan not found."})

    # Deactivate any existing active subscription
    db.query(AgentSubscription).filter(
        AgentSubscription.agent_id == agent.id,
        AgentSubscription.is_active == True,
    ).update({"is_active": False})

    expires = datetime.utcnow() + timedelta(days=30 * plan.duration_months)
    sub = AgentSubscription(
        agent_id=agent.id,
        plan_id=plan.id,
        expires_at=expires,
        is_active=True,
        payment_reference=payment_reference,
    )
    db.add(sub)

    # Update agent priority score
    agent.subscription_priority = plan.priority_boost
    db.commit()

    return {
        "message": f"Subscribed to {plan.display_name} plan. Active until {expires.date()}.",
        "expires_at": expires.isoformat(),
        "priority_boost": plan.priority_boost,
    }


@router.get("/earnings", summary="Agent views their earnings history")
def get_my_earnings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "AGENT_NOT_FOUND", "message": "No agent profile found."})

    earnings = db.query(AgentEarning).filter(
        AgentEarning.agent_id == agent.id
    ).order_by(AgentEarning.created_at.desc()).all()

    return {
        "total_earned": agent.total_earnings,
        "pending_payout": sum(e.agent_payout for e in earnings if e.status == "pending"),
        "earnings": [
            {
                "id": str(e.id),
                "gross_fee": e.gross_fee,
                "agent_payout": e.agent_payout,
                "platform_cut": e.platform_cut,
                "currency": e.currency,
                "status": e.status,
                "paid_at": e.paid_at.isoformat() if e.paid_at else None,
                "created_at": e.created_at.isoformat(),
                "request_id": str(e.request_id) if e.request_id else None,
            }
            for e in earnings
        ],
    }


# ─── Public listing ──────────────────────────────────────────────────────────

@router.get("/", summary="Browse approved agents (buyers/admins)")
def list_agents(
    specialty: Optional[str] = None,
    available_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Agent).filter(Agent.status == AgentStatus.approved)
    if available_only:
        q = q.filter(Agent.is_available == True)
    if specialty:
        try:
            q = q.filter(Agent.specialty == AgentSpecialty(specialty))
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={"error": "INVALID_SPECIALTY", "message": f"Valid options: {[s.value for s in AgentSpecialty]}"},
            )
    # Subscribed agents (higher priority_boost) appear first
    agents = q.order_by(Agent.subscription_priority.desc(), Agent.rating.desc()).all()
    return [_agent_to_dict(a) for a in agents]


@router.get("/me", summary="Get current user's agent profile")
def get_my_agent_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    if not agent:
        raise HTTPException(
            status_code=404,
            detail={"error": "AGENT_PROFILE_NOT_FOUND", "message": "You have not registered as an agent yet."},
        )
    return _agent_to_dict(agent)


@router.get("/{agent_id}", summary="Get a specific agent's public profile")
def get_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = db.query(Agent).filter(
        Agent.id == agent_id,
        Agent.status == AgentStatus.approved,
    ).first()
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "AGENT_NOT_FOUND", "message": "Agent not found."})
    return _agent_to_dict(agent)


# ─── Agent registration ───────────────────────────────────────────────────────

class AgentRegisterRequest(BaseModel):
    specialty: str
    specialty_details: str
    certifications: list = []
    id_document_s3_key: str


@router.post("/register", status_code=status.HTTP_201_CREATED, summary="Register as an agent")
def register_as_agent(
    payload: AgentRegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.is_kyc_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "KYC_REQUIRED",
                "message": "You must complete KYC verification before registering as an agent. Go to Dashboard → KYC Verification.",
            },
        )

    existing = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    if existing:
        if existing.status == AgentStatus.approved:
            raise HTTPException(status_code=409, detail={"error": "ALREADY_AN_AGENT", "message": "You are already an approved agent."})
        if existing.status == AgentStatus.pending:
            raise HTTPException(status_code=409, detail={"error": "APPLICATION_PENDING", "message": "Your application is already under review."})
        db.delete(existing)
        db.flush()

    try:
        specialty_enum = AgentSpecialty(payload.specialty)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_SPECIALTY", "message": f"Choose from: {[s.value for s in AgentSpecialty]}"},
        )

    if len(payload.specialty_details.strip()) < 50:
        raise HTTPException(status_code=422, detail={"error": "INSUFFICIENT_DETAILS", "message": "Provide at least 50 characters."})
    if not payload.id_document_s3_key:
        raise HTTPException(status_code=422, detail={"error": "MISSING_ID_DOCUMENT", "message": "ID document is required."})

    agent = Agent(
        user_id=str(current_user.id),
        specialty=specialty_enum,
        specialty_details=payload.specialty_details.strip(),
        certifications=payload.certifications,
        id_document_url=payload.id_document_s3_key,
        status=AgentStatus.pending,
    )
    db.add(agent)

    admins = db.query(User).filter(User.is_admin == True).all()
    for admin in admins:
        _notify(
            db, str(admin.id),
            "New Agent Application",
            f"{current_user.first_name} {current_user.last_name}".strip() + f" applied as a {payload.specialty} agent.",
        )

    db.commit()
    db.refresh(agent)
    return {"message": "Application submitted. You will be notified once reviewed.", "agent": _agent_to_dict(agent)}


@router.put("/me", summary="Update agent profile")
def update_agent_profile(
    specialty_details: Optional[str] = None,
    hourly_rate: Optional[float] = None,
    hourly_rate_currency: Optional[str] = None,
    portfolio_url: Optional[str] = None,
    is_available: Optional[bool] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "AGENT_NOT_FOUND", "message": "No agent profile found."})
    if agent.status != AgentStatus.approved:
        raise HTTPException(status_code=403, detail={"error": "AGENT_NOT_APPROVED", "message": "Only approved agents can update their profile."})

    if specialty_details is not None:
        if len(specialty_details.strip()) < 50:
            raise HTTPException(status_code=422, detail={"error": "INSUFFICIENT_DETAILS", "message": "At least 50 characters required."})
        agent.specialty_details = specialty_details.strip()
    if hourly_rate is not None:
        agent.hourly_rate = hourly_rate
    if hourly_rate_currency:
        agent.hourly_rate_currency = hourly_rate_currency.upper()
    if portfolio_url is not None:
        agent.portfolio_url = portfolio_url
    if is_available is not None:
        agent.is_available = is_available

    db.commit()
    return {"message": "Profile updated.", "agent": _agent_to_dict(agent)}


# ─── Buyer: request an agent for a transaction ────────────────────────────────

class RequestAgentPayload(BaseModel):
    transaction_id: str
    agent_id: str
    message: Optional[str] = None


@router.post("/request", status_code=status.HTTP_201_CREATED, summary="Buyer requests agent for a transaction")
def request_agent(
    payload: RequestAgentPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tx = db.query(Transaction).filter(Transaction.id == payload.transaction_id).first()
    if not tx:
        raise HTTPException(status_code=404, detail={"error": "TRANSACTION_NOT_FOUND", "message": "Transaction not found."})
    if str(tx.buyer_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail={"error": "NOT_YOUR_TRANSACTION", "message": "Only the buyer can request an agent."})

    existing_req = db.query(AgentRequest).filter(
        AgentRequest.transaction_id == payload.transaction_id
    ).first()
    if existing_req and existing_req.status in (AgentRequestStatus.active, AgentRequestStatus.pending):
        raise HTTPException(status_code=409, detail={"error": "AGENT_ALREADY_REQUESTED", "message": "An active agent request already exists."})

    agent = db.query(Agent).filter(
        Agent.id == payload.agent_id,
        Agent.status == AgentStatus.approved,
        Agent.is_available == True,
    ).first()
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "AGENT_NOT_AVAILABLE", "message": "Agent not found or unavailable."})

    # Calculate agent service fee
    tier = _calculate_agent_fee(db, tx.amount)
    fee_charged = None
    fee_currency = None
    agent_payout = None
    if tier:
        if tier.fee_type == "flat":
            fee_charged = tier.fee_amount
        else:
            fee_charged = round(tx.amount * tier.fee_amount / 100, 2)
        fee_currency = tier.currency
        agent_payout = round(fee_charged * tier.agent_payout_percent / 100, 2)

    req = AgentRequest(
        transaction_id=payload.transaction_id,
        buyer_id=str(current_user.id),
        agent_id=payload.agent_id,
        buyer_message=payload.message,
        status=AgentRequestStatus.pending,
        fee_charged=fee_charged,
        fee_currency=fee_currency,
        agent_payout_amount=agent_payout,
    )
    db.add(req)

    _notify(db, str(agent.user_id), "New Agent Request",
            f"You have a new verification request for transaction '{tx.title}'.")
    _notify(db, str(current_user.id), "Agent Request Sent",
            f"Your request has been sent. Agent fee: {fee_currency or ''} {fee_charged or 'N/A'}.")

    db.commit()
    db.refresh(req)
    return {"message": "Agent request sent.", "request": _request_to_dict(req)}


@router.get("/requests/my", summary="Get buyer's agent requests")
def get_my_requests(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    reqs = db.query(AgentRequest).filter(AgentRequest.buyer_id == str(current_user.id)).all()
    return [_request_to_dict(r) for r in reqs]


@router.delete("/request/{request_id}", summary="Buyer cancels agent request")
def cancel_request(
    request_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    req = db.query(AgentRequest).filter(AgentRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail={"error": "REQUEST_NOT_FOUND", "message": "Request not found."})
    if str(req.buyer_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail={"error": "NOT_YOUR_REQUEST", "message": "You can only cancel your own requests."})
    if req.status not in (AgentRequestStatus.pending, AgentRequestStatus.accepted):
        raise HTTPException(status_code=409, detail={"error": "CANNOT_CANCEL", "message": f"Cannot cancel a '{req.status}' request."})
    req.status = AgentRequestStatus.cancelled
    db.commit()
    return {"message": "Agent request cancelled."}


# ─── Agent: accept/decline requests, upload evidence ─────────────────────────

@router.get("/incoming/requests", summary="Agent views their incoming requests")
def get_incoming_requests(
    current_user: User = Depends(get_current_approved_agent),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    if not agent:
        raise HTTPException(status_code=404, detail={"error": "AGENT_NOT_FOUND", "message": "Agent profile not found."})
    reqs = db.query(AgentRequest).filter(AgentRequest.agent_id == str(agent.id)).all()
    return [_request_to_dict(r) for r in reqs]


@router.put("/request/{request_id}/respond", summary="Agent accepts or declines a request")
def respond_to_request(
    request_id: str,
    action: str,
    current_user: User = Depends(get_current_approved_agent),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    req = db.query(AgentRequest).filter(
        AgentRequest.id == request_id,
        AgentRequest.agent_id == str(agent.id),
    ).first()
    if not req:
        raise HTTPException(status_code=404, detail={"error": "REQUEST_NOT_FOUND", "message": "Request not found."})
    if req.status != AgentRequestStatus.pending:
        raise HTTPException(status_code=409, detail={"error": "INVALID_STATUS", "message": f"Request is already '{req.status}'."})
    if action not in ("accept", "decline"):
        raise HTTPException(status_code=422, detail={"error": "INVALID_ACTION", "message": "Action must be 'accept' or 'decline'."})

    req.status = AgentRequestStatus.active if action == "accept" else AgentRequestStatus.declined

    if action == "accept" and req.fee_charged and req.agent_payout_amount:
        earning = AgentEarning(
            agent_id=agent.id,
            request_id=req.id,
            gross_fee=req.fee_charged,
            agent_payout=req.agent_payout_amount,
            platform_cut=req.fee_charged - req.agent_payout_amount,
            currency=req.fee_currency or "USD",
            status="pending",
        )
        db.add(earning)

    _notify(db, str(req.buyer_id), "Agent Response",
            f"Agent {'accepted' if action == 'accept' else 'declined'} your verification request.")
    db.commit()
    return {"message": f"Request {req.status}.", "request": _request_to_dict(req)}


@router.post("/request/{request_id}/evidence", summary="Agent uploads verification evidence")
def upload_evidence(
    request_id: str,
    file: UploadFile = File(...),
    notes: Optional[str] = Form(None),
    current_user: User = Depends(get_current_approved_agent),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    req = db.query(AgentRequest).filter(
        AgentRequest.id == request_id,
        AgentRequest.agent_id == str(agent.id),
        AgentRequest.status == AgentRequestStatus.active,
    ).first()
    if not req:
        raise HTTPException(status_code=404, detail={"error": "REQUEST_NOT_FOUND", "message": "Active agent request not found."})

    content_type = file.content_type or "application/octet-stream"
    if content_type not in {"image/jpeg", "image/png", "image/webp", "application/pdf"}:
        raise HTTPException(status_code=422, detail={"error": "INVALID_FILE_TYPE", "message": "Evidence must be JPG, PNG, WebP, or PDF."})

    content = file.file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail={"error": "FILE_TOO_LARGE", "message": "Max 20 MB."})

    s3_key, _, _ = get_presigned_upload_url(
        folder="agent_evidence",
        filename=file.filename or "evidence",
        content_type=content_type,
        user_id=str(current_user.id),
    )
    save_local_upload(s3_key, content)

    keys = req.evidence_s3_keys or []
    keys.append({"key": s3_key, "notes": notes, "uploaded_at": datetime.utcnow().isoformat()})
    req.evidence_s3_keys = keys
    if notes:
        req.agent_notes = notes

    _notify(db, str(req.buyer_id), "Agent Uploaded Evidence",
            "Your assigned agent has uploaded verification evidence.")
    db.commit()
    return {"message": "Evidence uploaded.", "s3_key": s3_key}


@router.put("/request/{request_id}/complete", summary="Agent marks their work as complete")
def complete_request(
    request_id: str,
    final_notes: Optional[str] = None,
    current_user: User = Depends(get_current_approved_agent),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    req = db.query(AgentRequest).filter(
        AgentRequest.id == request_id,
        AgentRequest.agent_id == str(agent.id),
        AgentRequest.status == AgentRequestStatus.active,
    ).first()
    if not req:
        raise HTTPException(status_code=404, detail={"error": "REQUEST_NOT_FOUND", "message": "Active request not found."})

    req.status = AgentRequestStatus.completed
    req.completed_at = datetime.utcnow()
    if final_notes:
        req.agent_notes = final_notes
    agent.total_verifications += 1
    if req.agent_payout_amount:
        agent.total_earnings += req.agent_payout_amount

    _notify(db, str(req.buyer_id), "Agent Completed Verification",
            "Your agent has completed verification. Review the evidence in your transaction details.")
    db.commit()
    return {"message": "Verification marked complete.", "request": _request_to_dict(req)}


@router.get("/transaction/{transaction_id}/details", summary="Agent views transaction (read-only)")
def agent_view_transaction(
    transaction_id: str,
    current_user: User = Depends(get_current_approved_agent),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    req = db.query(AgentRequest).filter(
        AgentRequest.transaction_id == transaction_id,
        AgentRequest.agent_id == str(agent.id),
        AgentRequest.status == AgentRequestStatus.active,
    ).first()
    if not req:
        raise HTTPException(status_code=403, detail={"error": "ACCESS_DENIED", "message": "Not assigned to this transaction."})

    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    return {
        "id": tx.id,
        "title": tx.title,
        "description": tx.description,
        "amount": tx.amount,
        "currency": tx.currency,
        "status": tx.status,
        "type": tx.type,
        "expected_completion_date": tx.expected_completion_date.isoformat() if tx.expected_completion_date else None,
        "milestones": [
            {
                "id": m.id,
                "title": m.title,
                "description": m.description,
                "amount": m.amount,
                "status": m.status,
                "due_date": m.due_date.isoformat() if m.due_date else None,
            }
            for m in (tx.milestones or [])
        ],
        "seller_info": "REDACTED — Agents do not have access to seller contact details.",
    }
