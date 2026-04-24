import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Text, ForeignKey, Float, JSON
from sqlalchemy.orm import relationship
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    role = Column(String(20), nullable=False, default="buyer")  # buyer | seller
    avatar_url = Column(String, nullable=True)
    is_kyc_verified = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    is_email_verified = Column(Boolean, default=False)
    is_admin = Column(Boolean, default=False)
    is_agent = Column(Boolean, default=False)
    phone = Column(String(50), nullable=True)
    location = Column(String(200), nullable=True)
    website = Column(String(300), nullable=True)
    bio = Column(Text, nullable=True)
    country_code = Column(String(5), nullable=True)

    # ── Reputation ────────────────────────────────────────────────────────────
    rating = Column(Float, default=0.0)             # 0.0–5.0 average
    rating_count = Column(Float, default=0.0)       # number of ratings received
    completion_rate = Column(Float, default=0.0)    # % of transactions completed
    dispute_rate = Column(Float, default=0.0)       # % of transactions disputed
    total_volume = Column(Float, default=0.0)       # cumulative transaction value (NGN)
    badges = Column(JSON, default=list)             # ["kyc_verified", "trusted_seller", ...]

    # ── Risk & Fraud Controls ─────────────────────────────────────────────────
    wallet_frozen = Column(Boolean, default=False)        # admin: freeze all wallet ops
    withdrawals_blocked = Column(Boolean, default=False)  # admin: block withdrawals only
    withdrawal_cooldown_until = Column(DateTime, nullable=True)  # can't withdraw until this time
    risk_score = Column(Float, default=0.0)               # 0=low, 100=high risk

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    settings = relationship("UserSettings", back_populates="user", uselist=False, cascade="all, delete-orphan")
    transactions_as_buyer = relationship("Transaction", foreign_keys="Transaction.buyer_id", back_populates="buyer")
    transactions_as_seller = relationship("Transaction", foreign_keys="Transaction.seller_id", back_populates="seller")
    wallet_balances = relationship("WalletBalance", back_populates="user", cascade="all, delete-orphan")
    wallet_transactions = relationship("WalletTransaction", back_populates="user", cascade="all, delete-orphan")
    disputes = relationship("Dispute", back_populates="raised_by_user", cascade="all, delete-orphan")
    kyc_documents = relationship("KycDocument", back_populates="user", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
    sent_messages = relationship("DirectMessage", foreign_keys="DirectMessage.sender_id", back_populates="sender")
    received_messages = relationship("DirectMessage", foreign_keys="DirectMessage.recipient_id", back_populates="recipient")
    agent_profile = relationship("Agent", foreign_keys="Agent.user_id", back_populates="user", uselist=False)

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def completion_percentage(self):
        fields = [self.phone, self.location, self.website, self.bio, self.avatar_url]
        filled = sum(1 for f in fields if f)
        base = 60
        return base + int((filled / len(fields)) * 40)


class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    dark_mode = Column(String(10), default="system")
    email_notifications = Column(Boolean, default=True)
    push_notifications = Column(Boolean, default=True)
    sms_notifications = Column(Boolean, default=False)
    transaction_notifications = Column(Boolean, default=True)
    marketing_notifications = Column(Boolean, default=False)
    security_notifications = Column(Boolean, default=True)
    default_currency = Column(String(10), default="NGN")
    two_factor_enabled = Column(Boolean, default=False)
    two_factor_secret = Column(String, nullable=True)

    user = relationship("User", back_populates="settings")
