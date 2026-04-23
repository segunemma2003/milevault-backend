import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Text, ForeignKey, Enum, Float, Integer, JSON
from sqlalchemy.dialects.postgresql import UUID
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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    specialty = Column(Enum(AgentSpecialty), nullable=False)
    specialty_details = Column(Text, nullable=False)  # Detailed description
    certifications = Column(JSON, default=list)        # [{"name": "...", "issuer": "...", "year": 2022}]
    years_experience = Column(Integer, default=0)
    hourly_rate = Column(Float, default=0.0)
    hourly_rate_currency = Column(String(10), default="USD")
    portfolio_url = Column(String(500), nullable=True)
    id_document_url = Column(String(500), nullable=True)   # S3 key
    status = Column(Enum(AgentStatus), default=AgentStatus.pending, nullable=False)
    rejection_reason = Column(Text, nullable=True)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    is_available = Column(Boolean, default=True)
    total_verifications = Column(Integer, default=0)
    rating = Column(Float, default=0.0)
    rating_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id], back_populates="agent_profile")
    approver = relationship("User", foreign_keys=[approved_by])
    requests = relationship("AgentRequest", back_populates="agent", foreign_keys="AgentRequest.agent_id")


class AgentRequest(Base):
    """Buyer requests an agent to help verify/oversee a transaction."""
    __tablename__ = "agent_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id = Column(UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    buyer_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    status = Column(Enum(AgentRequestStatus), default=AgentRequestStatus.pending, nullable=False)
    buyer_message = Column(Text, nullable=True)     # Buyer's note to the agent
    agent_notes = Column(Text, nullable=True)       # Agent's internal notes (buyer/admin only)
    evidence_s3_keys = Column(JSON, default=list)   # Agent-uploaded evidence S3 keys
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transaction = relationship("Transaction", back_populates="agent_request")
    buyer = relationship("User", foreign_keys=[buyer_id])
    agent = relationship("Agent", back_populates="requests", foreign_keys=[agent_id])
