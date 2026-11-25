import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, DateTime, ForeignKey, Boolean, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


class ProcessedEvent(Base):
    """
    Exactly-once guard for money events. A row is inserted in the SAME database
    transaction as the balance change; the unique (source, event_id) pair makes a
    second attempt (webhook retry, double click, reconciliation re-run) a no-op.
    """
    __tablename__ = "processed_events"
    __table_args__ = (UniqueConstraint("source", "event_id", name="uq_processed_events_source_event"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    source = Column(String(60), nullable=False)      # e.g. "deposit:paystack", "milestone_release"
    event_id = Column(String(255), nullable=False)   # provider tx id / reference / our own key
    created_at = Column(DateTime, default=datetime.utcnow)


class WalletBalance(Base):
    __tablename__ = "wallet_balances"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    currency = Column(String(10), nullable=False)
    amount = Column(Float, default=0.0)           # available (spendable)
    escrow_amount = Column(Float, default=0.0)    # locked in active milestones
    pending_amount = Column(Float, default=0.0)   # pending withdrawal
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="wallet_balances")


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(20), nullable=False)
    # deposit | withdrawal | transfer_in | transfer_out
    # escrow_lock | escrow_release | escrow_refund
    # refund | conversion
    amount = Column(Float, nullable=False)
    fee = Column(Float, nullable=True)  # withdrawal fee kept by the platform (paid out = amount - fee)
    currency = Column(String(10), nullable=False)
    status = Column(String(20), default="pending")  # pending | completed | failed | cancelled
    description = Column(String(500), nullable=True)
    transaction_id = Column(String, ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True)
    milestone_id = Column(String, nullable=True)   # which milestone this escrow entry belongs to
    method = Column(String(50), nullable=True)
    reference = Column(String(100), nullable=True)
    flagged_high_risk = Column(Boolean, default=False)  # admin queue: large amount / risk_score / dispute pattern
    # Withdrawals to a saved destination (PayoutDestination). status: pending (awaiting approval)
    # → processing (sent to provider) → completed | failed — completion only via provider webhook.
    payout_destination_id = Column(String, nullable=True)
    provider_payout_id = Column(String(120), nullable=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="wallet_transactions")
    linked_transaction = relationship("Transaction", back_populates="wallet_transactions")


class LedgerEntry(Base):
    """Double-entry ledger: every financial movement records both sides."""
    __tablename__ = "ledger_entries"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    debit_user_id = Column(String, nullable=True)    # who loses funds (None = platform)
    credit_user_id = Column(String, nullable=True)   # who gains funds (None = platform)
    debit_account = Column(String(30), nullable=False)   # available | escrow | pending | platform
    credit_account = Column(String(30), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False)
    reference_type = Column(String(30), nullable=False)  # milestone_fund | milestone_release | withdrawal | deposit | refund | transfer
    reference_id = Column(String, nullable=True)         # milestone_id / transaction_id / withdrawal_id
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
