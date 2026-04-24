import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Text, ForeignKey, Enum, Float, Integer, JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base
import enum


class CurrencyType(str, enum.Enum):
    fiat = "fiat"
    crypto = "crypto"


class PaymentGatewayName(str, enum.Enum):
    paystack = "paystack"       # Nigeria, Ghana, Kenya, South Africa
    stripe = "stripe"           # US, EU, UK, etc.
    flutterwave = "flutterwave" # Africa-wide
    coinbase = "coinbase"       # Crypto
    manual = "manual"           # Manual bank transfer


class Currency(Base):
    """Admin-managed currencies (fiat + crypto)."""
    __tablename__ = "currencies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code = Column(String(10), unique=True, nullable=False, index=True)   # USD, EUR, BTC, NGN
    name = Column(String(100), nullable=False)
    symbol = Column(String(10), nullable=False)                          # $, €, ₿, ₦
    type = Column(Enum(CurrencyType), default=CurrencyType.fiat, nullable=False)
    decimal_places = Column(Integer, default=2)
    is_active = Column(Boolean, default=True)
    is_base = Column(Boolean, default=False)  # One base currency (USD) for rate reference
    created_by = Column(String, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    exchange_rates_from = relationship("ExchangeRate", foreign_keys="ExchangeRate.from_currency_id", back_populates="from_currency")
    exchange_rates_to = relationship("ExchangeRate", foreign_keys="ExchangeRate.to_currency_id", back_populates="to_currency")


class ExchangeRate(Base):
    """Admin-controlled exchange rates. Rate = how many to_currency per 1 from_currency."""
    __tablename__ = "exchange_rates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_currency_id = Column(UUID(as_uuid=True), ForeignKey("currencies.id"), nullable=False)
    to_currency_id = Column(UUID(as_uuid=True), ForeignKey("currencies.id"), nullable=False)
    rate = Column(Float, nullable=False)           # 1 from = rate * to
    spread_percent = Column(Float, default=0.5)   # Platform spread (0.5%)
    is_active = Column(Boolean, default=True)
    set_by = Column(String, ForeignKey("users.id"), nullable=True)
    valid_from = Column(DateTime, default=datetime.utcnow)
    valid_to = Column(DateTime, nullable=True)    # None = indefinite
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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(Enum(PaymentGatewayName), nullable=False)
    display_name = Column(String(100), nullable=False)
    country_codes = Column(JSON, default=list)    # ["NG", "GH", "KE"] or ["*"] for all
    supported_currencies = Column(JSON, default=list)  # ["NGN", "GHS", "USD"]
    is_active = Column(Boolean, default=True)
    supports_deposit = Column(Boolean, default=True)
    supports_withdrawal = Column(Boolean, default=True)
    min_amount = Column(Float, default=1.0)
    max_amount = Column(Float, default=100000.0)
    fee_percent = Column(Float, default=1.5)
    fee_fixed = Column(Float, default=0.0)
    priority = Column(Integer, default=10)        # Lower = higher priority in routing
    config_json = Column(JSON, default=dict)     # Non-secret config (logo, docs url, etc.)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Refund(Base):
    """Admin-only refund after dispute review."""
    __tablename__ = "refunds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id = Column(UUID(as_uuid=True), ForeignKey("transactions.id"), nullable=False)
    dispute_id = Column(UUID(as_uuid=True), ForeignKey("disputes.id"), nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False)
    refund_to = Column(String, ForeignKey("users.id"), nullable=False)   # Who gets the money
    reason = Column(Text, nullable=False)
    admin_notes = Column(Text, nullable=True)
    status = Column(String(20), default="pending")    # pending | processing | completed | failed
    processed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
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

    # Escrow fee charged on each deal
    escrow_fee_percent = Column(Float, default=2.5)      # 2.5% default
    min_fee_amount = Column(Float, default=1.0)          # Never charge less than $1
    max_fee_amount = Column(Float, nullable=True)        # None = no cap
    fee_currency = Column(String(10), default="USD")     # Currency the cap/min is expressed in

    # Who pays the fee
    fee_paid_by = Column(String(10), default="buyer")    # buyer | seller | split
    buyer_fee_share = Column(Float, default=100.0)       # % of fee buyer pays (100 = buyer pays all)
    seller_fee_share = Column(Float, default=0.0)

    # Withdrawal fee
    withdrawal_fee_percent = Column(Float, default=0.5)
    withdrawal_fee_fixed = Column(Float, default=0.0)

    # Misc
    max_transaction_amount = Column(Float, nullable=True)  # None = unlimited
    min_transaction_amount = Column(Float, default=1.0)
    platform_name = Column(String(100), default="MileVault")
    support_email = Column(String(255), default="support@milevault.com")

    updated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
