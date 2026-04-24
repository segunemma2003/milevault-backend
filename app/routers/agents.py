"""
Agent router — registration, specialty management, buyer requests.
Rules enforced here:
  - Agents CANNOT communicate with sellers or see seller private details.
  - Buyers request agents; agents accept/decline.
  - Agents upload evidence visible only to buyer + admin.
  - No agent ↔ seller direct access at any layer.
"""
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, status, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.agent import Agent, AgentRequest, AgentSpecialty, AgentStatus, AgentRequestStatus
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
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
    }
    if include_user and agent.user:
        d["name"] = f"{agent.user.first_name} {agent.user.last_name}".strip() if agent.user else ""
        d["avatar_url"] = agent.user.avatar_url
    return d


def _request_to_dict(req: AgentRequest) -> dict:
    return {
        "id": str(req.id),
        "transaction_id": str(req.transaction_id),
        "buyer_id": str(req.buyer_id),
        "agent_id": str(req.agent_id) if req.agent_id else None,
        "status": req.status,
        "buyer_message": req.buyer_message,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "agent": _agent_to_dict(req.agent) if req.agent else None,
    }


def _notify(db: Session, user_id: str, title: str, message: str, ntype: str = "agent"):
    db.add(Notification(user_id=user_id, title=title, message=message, type=ntype))


# ─── Public listing ──────────────────────────────────────────────────────────

@router.get("/", summary="Browse approved agents (buyers/admins)")
def list_agents(
    specialty: Optional[str] = None,
    available_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all approved agents. Buyers use this to find and request agents."""
    cache_key = f"agents:list:{specialty}:{available_only}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    q = db.query(Agent).filter(Agent.status == AgentStatus.approved)
    if available_only:
        q = q.filter(Agent.is_available == True)
    if specialty:
        try:
            q = q.filter(Agent.specialty == AgentSpecialty(specialty))
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "INVALID_SPECIALTY",
                    "message": f"'{specialty}' is not a valid specialty. Valid options: {[s.value for s in AgentSpecialty]}",
                },
            )

    agents = q.all()
    result = [_agent_to_dict(a) for a in agents]
    cache_set(cache_key, result, ttl=120)
    return result


@router.get("/me", summary="Get current user's agent profile")
def get_my_agent_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    if not agent:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "AGENT_PROFILE_NOT_FOUND",
                "message": "You have not registered as an agent yet. Use POST /agents/register to apply.",
            },
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
        raise HTTPException(
            status_code=404,
            detail={"error": "AGENT_NOT_FOUND", "message": "Agent not found or not yet approved."},
        )
    return _agent_to_dict(agent)


# ─── Agent registration ───────────────────────────────────────────────────────

class AgentRegisterRequest(BaseModel):
    specialty: str
    specialty_details: str
    certifications: list = []
    id_document_s3_key: str  # Client uploads via /uploads/presign first


@router.post("/register", status_code=status.HTTP_201_CREATED, summary="Register as an agent")
def register_as_agent(
    payload: AgentRegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Apply to become an agent. Client must upload the ID document via /uploads/presign
    first and pass the resulting s3_key as id_document_s3_key.
    An admin must approve before the agent can accept requests.
    """
    existing = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    if existing:
        if existing.status == AgentStatus.approved:
            raise HTTPException(
                status_code=409,
                detail={"error": "ALREADY_AN_AGENT", "message": "You are already a registered and approved agent."},
            )
        if existing.status == AgentStatus.pending:
            raise HTTPException(
                status_code=409,
                detail={"error": "APPLICATION_PENDING", "message": "Your agent application is already under review. Please wait for admin approval."},
            )
        db.delete(existing)
        db.flush()

    try:
        specialty_enum = AgentSpecialty(payload.specialty)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "INVALID_SPECIALTY",
                "message": f"'{payload.specialty}' is not valid. Choose from: {[s.value for s in AgentSpecialty]}",
            },
        )

    if len(payload.specialty_details.strip()) < 50:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "INSUFFICIENT_DETAILS",
                "message": "Please provide at least 50 characters describing your specialty and experience.",
            },
        )

    if not payload.id_document_s3_key:
        raise HTTPException(
            status_code=422,
            detail={"error": "MISSING_ID_DOCUMENT", "message": "A government-issued ID document is required."},
        )

    agent = Agent(
        user_id=str(current_user.id),
        specialty=specialty_enum,
        specialty_details=payload.specialty_details.strip(),
        certifications=payload.certifications,
        id_document_url=payload.id_document_s3_key,
        status=AgentStatus.pending,
    )
    db.add(agent)

    # Notify admins
    admins = db.query(User).filter(User.is_admin == True).all()
    for admin in admins:
        _notify(
            db, str(admin.id),
            "New Agent Application",
            f"{current_user.first_name} {current_user.last_name}".strip() + f" has applied to be a {specialty} agent. Review in the admin panel.",
        )

    db.commit()
    db.refresh(agent)
    return {
        "message": "Agent application submitted successfully. You will be notified once an admin reviews your application.",
        "agent": _agent_to_dict(agent),
    }


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
        raise HTTPException(
            status_code=404,
            detail={"error": "AGENT_NOT_FOUND", "message": "No agent profile found. Please register first."},
        )
    if agent.status != AgentStatus.approved:
        raise HTTPException(
            status_code=403,
            detail={"error": "AGENT_NOT_APPROVED", "message": "Only approved agents can update their profile."},
        )

    if specialty_details is not None:
        if len(specialty_details.strip()) < 50:
            raise HTTPException(
                status_code=422,
                detail={"error": "INSUFFICIENT_DETAILS", "message": "Specialty details must be at least 50 characters."},
            )
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

@router.post("/request", status_code=status.HTTP_201_CREATED, summary="Buyer requests agent for a transaction")
def request_agent(
    transaction_id: str,
    agent_id: str,
    message: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Buyers only. Optionally attaches an approved agent to their transaction.
    The agent gets read-only access and can upload verification evidence.
    Sellers cannot see agent details or communicate with agents.
    """
    tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not tx:
        raise HTTPException(
            status_code=404,
            detail={"error": "TRANSACTION_NOT_FOUND", "message": "Transaction not found."},
        )
    if str(tx.buyer_id) != str(current_user.id):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "NOT_YOUR_TRANSACTION",
                "message": "Only the buyer of this transaction can request an agent.",
            },
        )

    existing_req = db.query(AgentRequest).filter(
        AgentRequest.transaction_id == transaction_id
    ).first()
    if existing_req and existing_req.status in (AgentRequestStatus.active, AgentRequestStatus.pending):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "AGENT_ALREADY_REQUESTED",
                "message": "An agent request already exists for this transaction. Cancel it before creating a new one.",
            },
        )

    agent = db.query(Agent).filter(
        Agent.id == agent_id,
        Agent.status == AgentStatus.approved,
        Agent.is_available == True,
    ).first()
    if not agent:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "AGENT_NOT_AVAILABLE",
                "message": "Agent not found, not approved, or currently unavailable.",
            },
        )

    req = AgentRequest(
        transaction_id=transaction_id,
        buyer_id=str(current_user.id),
        agent_id=agent_id,
        buyer_message=message,
        status=AgentRequestStatus.pending,
    )
    db.add(req)

    _notify(
        db, str(agent.user_id),
        "New Agent Request",
        f"You have a new verification request for transaction '{tx.title}'. Log in to accept or decline.",
    )
    _notify(
        db, str(current_user.id),
        "Agent Request Sent",
        ("Your request to agent " + (f"{agent.user.first_name} {agent.user.last_name}".strip() if agent.user else "") + " has been sent. Awaiting their response."),
    )

    db.commit()
    db.refresh(req)
    return {"message": "Agent request sent successfully.", "request": _request_to_dict(req)}


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
        raise HTTPException(status_code=404, detail={"error": "REQUEST_NOT_FOUND", "message": "Agent request not found."})
    if str(req.buyer_id) != str(current_user.id):
        raise HTTPException(
            status_code=403,
            detail={"error": "NOT_YOUR_REQUEST", "message": "You can only cancel your own agent requests."},
        )
    if req.status not in (AgentRequestStatus.pending, AgentRequestStatus.accepted):
        raise HTTPException(
            status_code=409,
            detail={"error": "CANNOT_CANCEL", "message": f"Cannot cancel a request with status '{req.status}'."},
        )
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
    action: str,  # "accept" | "decline"
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
        raise HTTPException(
            status_code=409,
            detail={"error": "INVALID_STATUS", "message": f"Request is already '{req.status}' and cannot be responded to."},
        )
    if action not in ("accept", "decline"):
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_ACTION", "message": "Action must be 'accept' or 'decline'."},
        )

    req.status = AgentRequestStatus.active if action == "accept" else AgentRequestStatus.declined
    _notify(
        db, str(req.buyer_id),
        "Agent Response",
        f"Agent {'accepted' if action == 'accept' else 'declined'} your request for transaction.",
    )
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
    """
    Agent uploads verification documents/images for the buyer to review.
    SELLER CANNOT ACCESS THIS. Enforced by agent-only dependency.
    """
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    req = db.query(AgentRequest).filter(
        AgentRequest.id == request_id,
        AgentRequest.agent_id == str(agent.id),
        AgentRequest.status == AgentRequestStatus.active,
    ).first()
    if not req:
        raise HTTPException(
            status_code=404,
            detail={"error": "REQUEST_NOT_FOUND", "message": "Active agent request not found."},
        )

    content_type = file.content_type or "application/octet-stream"
    allowed = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
    if content_type not in allowed:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_FILE_TYPE", "message": "Evidence must be JPG, PNG, WebP, or PDF."},
        )

    content = file.file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail={"error": "FILE_TOO_LARGE", "message": "Evidence file must not exceed 20 MB."},
        )

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

    _notify(
        db, str(req.buyer_id),
        "Agent Uploaded Evidence",
        "Your assigned agent has uploaded verification evidence for your transaction.",
    )
    db.commit()
    return {"message": "Evidence uploaded successfully.", "s3_key": s3_key}


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

    _notify(
        db, str(req.buyer_id),
        "Agent Completed Verification",
        "Your agent has completed the verification for your transaction. Review the evidence in your transaction details.",
    )
    db.commit()
    return {"message": "Verification marked as complete.", "request": _request_to_dict(req)}


@router.get("/transaction/{transaction_id}/details", summary="Agent views transaction (read-only, buyer/agent access only)")
def agent_view_transaction(
    transaction_id: str,
    current_user: User = Depends(get_current_approved_agent),
    db: Session = Depends(get_db),
):
    """
    Agents get read-only access to the transaction they're assigned to.
    Critical: seller details are redacted; agents cannot see seller contact info.
    """
    agent = db.query(Agent).filter(Agent.user_id == str(current_user.id)).first()
    req = db.query(AgentRequest).filter(
        AgentRequest.transaction_id == transaction_id,
        AgentRequest.agent_id == str(agent.id),
        AgentRequest.status == AgentRequestStatus.active,
    ).first()
    if not req:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "ACCESS_DENIED",
                "message": "You are not assigned to this transaction or the request is not active.",
            },
        )

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
        # Seller identity is HIDDEN from agents
        "seller_info": "REDACTED — Agents do not have access to seller contact details.",
    }
