import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Text, ForeignKey, Enum, Float, Integer, JSON
from sqlalchemy.orm import relationship
from app.database import Base
import enum


class AgentSpecialty(str, enum.Enum):
    real_estate = "real_estate"
    software = "software"
    legal = "legal"
    financial = "financial"
    construction = "construction"
    healthcare = "healthcare"
    education = "education"
    logistics = "logistics"
    general = "general"


class AgentStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    suspended = "suspended"


class AgentRequestStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    active = "active"
    completed = "completed"
    cancelled = "cancelled"


class Agent(Base):
    __tablename__ = "agents"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    specialty = Column(Enum(AgentSpecialty), nullable=False)
    specialty_details = Column(Text, nullable=False)
    certifications = Column(JSON, default=list)
    years_experience = Column(Integer, default=0)
    hourly_rate = Column(Float, default=0.0)
    hourly_rate_currency = Column(String(10), default="USD")
    portfolio_url = Column(String(500), nullable=True)
    id_document_url = Column(String(500), nullable=True)
    status = Column(Enum(AgentStatus), default=AgentStatus.pending, nullable=False)
    rejection_reason = Column(Text, nullable=True)
    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    is_available = Column(Boolean, default=True)
    total_verifications = Column(Integer, default=0)
    rating = Column(Float, default=0.0)
    rating_count = Column(Integer, default=0)
    subscription_priority = Column(Integer, default=0)
    total_earnings = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id], back_populates="agent_profile")
    approver = relationship("User", foreign_keys=[approved_by])
    requests = relationship("AgentRequest", back_populates="agent", foreign_keys="AgentRequest.agent_id")
    subscriptions = relationship("AgentSubscription", back_populates="agent", cascade="all, delete-orphan")
    earnings = relationship("AgentEarning", back_populates="agent", cascade="all, delete-orphan")


class AgentRequest(Base):
    """Buyer (or admin) requests an agent to help verify/oversee a transaction."""
    __tablename__ = "agent_requests"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id = Column(String, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    buyer_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    agent_id = Column(String, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    status = Column(Enum(AgentRequestStatus), default=AgentRequestStatus.pending, nullable=False)
    buyer_message = Column(Text, nullable=True)
    agent_notes = Column(Text, nullable=True)
    evidence_s3_keys = Column(JSON, default=list)
    fee_charged = Column(Float, nullable=True)
    fee_currency = Column(String(10), nullable=True)
    agent_payout_amount = Column(Float, nullable=True)
    payout_status = Column(String(20), default="pending")
    assigned_by_admin = Column(Boolean, default=False)
    assigned_by = Column(String, ForeignKey("users.id"), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transaction = relationship("Transaction", back_populates="agent_request")
    buyer = relationship("User", foreign_keys=[buyer_id])
    agent = relationship("Agent", back_populates="requests", foreign_keys=[agent_id])
    assigner = relationship("User", foreign_keys=[assigned_by])
    earning = relationship("AgentEarning", back_populates="request", uselist=False)
    messages = relationship("AgentRequestMessage", back_populates="request", cascade="all, delete-orphan")


class AgentRequestMessage(Base):
    """
    Mediated Q&A on an agent request: no direct seller↔agent private channel.
    Visible to buyer, seller (party to tx), assigned agent, and admins via API rules.
    """
    __tablename__ = "agent_request_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_request_id = Column(String, ForeignKey("agent_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    author_user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # buyer | seller | agent | admin — who spoke (for audit); must match author_user_id role on tx
    author_role = Column(String(20), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    request = relationship("AgentRequest", back_populates="messages")


class AgentSubscriptionPlan(Base):
    """Admin-defined monthly subscription tiers for agents."""
    __tablename__ = "agent_subscription_plans"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(50), nullable=False, unique=True)
    display_name = Column(String(100), nullable=False)
    price = Column(Float, nullable=False)
    currency = Column(String(10), default="USD")
    duration_months = Column(Integer, default=1)
    priority_boost = Column(Integer, default=0)
    features = Column(JSON, default=list)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    subscriptions = relationship("AgentSubscription", back_populates="plan")


class AgentSubscription(Base):
    """Agent's active (or past) subscription to a plan."""
    __tablename__ = "agent_subscriptions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id = Column(String, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    plan_id = Column(String, ForeignKey("agent_subscription_plans.id"), nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True)
    payment_reference = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    agent = relationship("Agent", back_populates="subscriptions")
    plan = relationship("AgentSubscriptionPlan", back_populates="subscriptions")


class AgentServiceTier(Base):
    """Admin-configured agent service fee by transaction value range."""
    __tablename__ = "agent_service_tiers"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    min_transaction_amount = Column(Float, nullable=False, default=0.0)
    max_transaction_amount = Column(Float, nullable=True)
    fee_type = Column(String(10), default="flat")
    fee_amount = Column(Float, nullable=False)
    agent_payout_percent = Column(Float, default=70.0)
    currency = Column(String(10), default="USD")
    description = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentEarning(Base):
    """Per-job earning record for an agent."""
    __tablename__ = "agent_earnings"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id = Column(String, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    request_id = Column(String, ForeignKey("agent_requests.id", ondelete="SET NULL"), nullable=True)
    gross_fee = Column(Float, nullable=False)
    agent_payout = Column(Float, nullable=False)
    platform_cut = Column(Float, nullable=False)
    currency = Column(String(10), default="USD")
    status = Column(String(20), default="pending")
    paid_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    agent = relationship("Agent", back_populates="earnings")
    request = relationship("AgentRequest", back_populates="earning")
