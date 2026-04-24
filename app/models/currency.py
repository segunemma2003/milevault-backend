import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Text, ForeignKey, Enum, Float, Integer, JSON, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base
import enum


class CurrencyType(str, enum.Enum):
    fiat = "fiat"
    crypto = "crypto"


class PaymentGatewayName(str, enum.Enum):
    paystack = "paystack"
    stripe = "stripe"
    flutterwave = "flutterwave"
    coinbase = "coinbase"
    manual = "manual"


class Currency(Base):
    """Admin-managed currencies (fiat + crypto)."""
    __tablename__ = "currencies"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    code = Column(String(10), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    symbol = Column(String(10), nullable=False)
    type = Column(Enum(CurrencyType), default=CurrencyType.fiat, nullable=False)
    decimal_places = Column(Integer, default=2)
    is_active = Column(Boolean, default=True)
    is_base = Column(Boolean, default=False)
    created_by = Column(String, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    exchange_rates_from = relationship("ExchangeRate", foreign_keys="ExchangeRate.from_currency_id", back_populates="from_currency")
    exchange_rates_to = relationship("ExchangeRate", foreign_keys="ExchangeRate.to_currency_id", back_populates="to_currency")


class ExchangeRate(Base):
    """Admin-controlled exchange rates. Rate = how many to_currency per 1 from_currency."""
    __tablename__ = "exchange_rates"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    from_currency_id = Column(String, ForeignKey("currencies.id"), nullable=False)
    to_currency_id = Column(String, ForeignKey("currencies.id"), nullable=False)
    rate = Column(Float, nullable=False)
    spread_percent = Column(Float, default=0.5)
    is_active = Column(Boolean, default=True)
    set_by = Column(String, ForeignKey("users.id"), nullable=True)
    valid_from = Column(DateTime, default=datetime.utcnow)
    valid_to = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("from_currency_id", "to_currency_id", name="uq_exchange_rate_pair"),
    )

    from_currency = relationship("Currency", foreign_keys=[from_currency_id], back_populates="exchange_rates_from")
    to_currency = relationship("Currency", foreign_keys=[to_currency_id], back_populates="exchange_rates_to")
    setter = relationship("User", foreign_keys=[set_by])


class PaymentGateway(Base):
    """Payment gateways available per country."""
    __tablename__ = "payment_gateways"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(Enum(PaymentGatewayName), nullable=False)
    display_name = Column(String(100), nullable=False)
    country_codes = Column(JSON, default=list)
    supported_currencies = Column(JSON, default=list)
    is_active = Column(Boolean, default=True)
    supports_deposit = Column(Boolean, default=True)
    supports_withdrawal = Column(Boolean, default=True)
    min_amount = Column(Float, default=1.0)
    max_amount = Column(Float, default=100000.0)
    fee_percent = Column(Float, default=1.5)
    fee_fixed = Column(Float, default=0.0)
    priority = Column(Integer, default=10)
    config_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Refund(Base):
    """Admin-only refund after dispute review."""
    __tablename__ = "refunds"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id = Column(String, ForeignKey("transactions.id"), nullable=False)
    dispute_id = Column(String, ForeignKey("disputes.id"), nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False)
    refund_to = Column(String, ForeignKey("users.id"), nullable=False)
    reason = Column(Text, nullable=False)
    admin_notes = Column(Text, nullable=True)
    status = Column(String(20), default="pending")
    processed_by = Column(String, ForeignKey("users.id"), nullable=True)
    processed_at = Column(DateTime, nullable=True)
    gateway_reference = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    transaction = relationship("Transaction", foreign_keys=[transaction_id])
    recipient = relationship("User", foreign_keys=[refund_to])
    processor = relationship("User", foreign_keys=[processed_by])


class PlatformSettings(Base):
    """
    Singleton table (always one row with id='default') for platform-wide fee config.
    Admin manages via /admin/settings.
    """
    __tablename__ = "platform_settings"

    id = Column(String(20), primary_key=True, default="default")

    escrow_fee_percent = Column(Float, default=2.5)
    min_fee_amount = Column(Float, default=1.0)
    max_fee_amount = Column(Float, nullable=True)
    fee_currency = Column(String(10), default="USD")

    fee_paid_by = Column(String(10), default="buyer")
    buyer_fee_share = Column(Float, default=100.0)
    seller_fee_share = Column(Float, default=0.0)

    withdrawal_fee_percent = Column(Float, default=0.5)
    withdrawal_fee_fixed = Column(Float, default=0.0)

    max_transaction_amount = Column(Float, nullable=True)
    min_transaction_amount = Column(Float, default=1.0)
    platform_name = Column(String(100), default="MileVault")
    support_email = Column(String(255), default="support@milevault.com")

    # When milestone amount >= this (same currency as milestone or tx), buyer must confirm checklist on approve
    high_value_checklist_threshold = Column(Float, nullable=True)

    # Days after seller accepts until funding deadline job runs (1–366)
    funding_deadline_days = Column(Integer, default=14, nullable=False)
    # Buyer inactivity window after delivery before auto-release (3–7)
    auto_release_days = Column(Integer, default=5, nullable=False)

    updated_by = Column(String, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
