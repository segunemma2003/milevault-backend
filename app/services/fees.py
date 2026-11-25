"""
Platform fees — every rate is set by an admin or the finance team (Admin → Platform Settings).

  Escrow fee   % of the deal (min/max in the fee currency, USD), paid by the buyer, the seller or
               split by the admin's shares. Locked onto each milestone when the deal is created,
               so changing the rate later never changes a running deal.
                 • buyer's share is paid together with the milestone and held in escrow — so every
                   refund path (which refunds what was escrowed) returns it automatically;
                 • seller's share comes out of each release.
  Withdrawal   % of the amount + a fixed amount, deducted from what is paid out.
  Dispute fee  fixed + % of the disputed amount, paid by whoever opens the dispute.

Fees leave the users' balances into the "platform_fees" ledger account (revenue).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import Session

from app.services import ledger

FEES_ACCOUNT = "platform_fees"
CENT = Decimal("0.01")


def platform_settings(db: Session):
    from app.models.currency import PlatformSettings
    s = db.query(PlatformSettings).filter(PlatformSettings.id == "default").first()
    if not s:
        s = PlatformSettings(id="default")
        db.add(s)
        db.flush()
    return s


def _d(v) -> Decimal:
    return ledger.to_decimal(v or 0)


def _money(v) -> Decimal:
    return _d(v).quantize(CENT, rounding=ROUND_HALF_UP)


def payer_shares(db: Session) -> tuple[Decimal, Decimal]:
    """(buyer %, seller %) from the admin's "fee paid by" setting."""
    s = platform_settings(db)
    who = (s.fee_paid_by or "buyer").lower()
    if who == "seller":
        return Decimal(0), Decimal(100)
    if who == "split":
        b = max(Decimal(0), min(Decimal(100), _d(s.buyer_fee_share if s.buyer_fee_share is not None else 50)))
        return b, Decimal(100) - b
    return Decimal(100), Decimal(0)


def deal_fee(db: Session, amount_usd) -> Decimal:
    s = platform_settings(db)
    pct = _d(s.escrow_fee_percent)
    if pct <= 0 or _d(amount_usd) <= 0:
        return Decimal(0)
    fee = _d(amount_usd) * pct / 100
    if s.min_fee_amount:
        fee = max(fee, _d(s.min_fee_amount))
    if s.max_fee_amount:
        fee = min(fee, _d(s.max_fee_amount))
    return _money(min(fee, _d(amount_usd)))


def allocate_deal_fees(db: Session, tx, milestones: list) -> None:
    """Split the deal's fee across its milestones (by amount) and between buyer and seller."""
    total = _d(tx.amount)
    fee = deal_fee(db, total)
    b_pct, s_pct = payer_shares(db)
    tx.service_fee_payment = "buyer" if s_pct == 0 else "seller" if b_pct == 0 else "split"
    tx.buyer_fee_ratio, tx.seller_fee_ratio = float(b_pct), float(s_pct)
    left = fee
    for i, m in enumerate(milestones):
        part = left if i == len(milestones) - 1 else _money(fee * _d(m.amount) / total) if total else Decimal(0)
        left -= part
        buyer = _money(part * b_pct / 100)
        m.buyer_fee = float(buyer)
        m.seller_fee = float(part - buyer)


def fund_target(m) -> float:
    """What the buyer pays into escrow for a milestone: its price + the buyer's fee share."""
    return round(float(m.amount) + float(getattr(m, "buyer_fee", 0) or 0), 8)


def withdrawal_fee(db: Session, amount, currency: str) -> Decimal:
    s = platform_settings(db)
    fee = _d(amount) * _d(s.withdrawal_fee_percent) / 100
    fixed = _d(s.withdrawal_fee_fixed)
    if fixed > 0:
        fee_cur = (s.fee_currency or "USD").upper()
        if fee_cur != currency.upper():
            from app.routers.transactions import _mid_rate
            fixed = fixed * _d(_mid_rate(db, fee_cur, currency.upper()))
        fee += fixed
    return _money(max(Decimal(0), fee))


def dispute_fee(db: Session, disputed_amount_usd) -> Decimal:
    s = platform_settings(db)
    fee = _d(getattr(s, "dispute_fee_fixed", 0)) + _d(disputed_amount_usd) * _d(getattr(s, "dispute_fee_percent", 0)) / 100
    return _money(max(Decimal(0), fee))


def collect(db: Session, *, user_id: str, currency: str, amount, from_bucket: str, reference_type: str,
            reference_id: str, description: str, idempotency_key: str) -> bool:
    """Move a fee out of a user's balance into platform revenue. Exactly once per key."""
    if _d(amount) <= 0:
        return False
    return ledger.debit(db, user_id=user_id, currency=currency, amount=_d(amount), from_bucket=from_bucket,
                        reference_type=reference_type, reference_id=reference_id, description=description,
                        idempotency_key=idempotency_key, external_account=FEES_ACCOUNT)


def debit_withdrawal(db: Session, txn) -> None:
    """A withdrawal was paid out: the net left the platform, the fee stays as revenue."""
    fee = _d(getattr(txn, "fee", 0))
    net = _d(txn.amount) - fee
    ledger.debit(db, user_id=txn.user_id, currency=txn.currency, amount=net, from_bucket="pending",
                 reference_type="withdrawal_paid", reference_id=str(txn.id),
                 description="Withdrawal paid out", idempotency_key=str(txn.id))
    if fee > 0:
        collect(db, user_id=txn.user_id, currency=txn.currency, amount=fee, from_bucket="pending",
                reference_type="withdrawal_fee", reference_id=str(txn.id),
                description="Withdrawal fee", idempotency_key=f"{txn.id}:fee")


def collect_from_any_balance(db: Session, *, user_id: str, amount, currency: str, reference_type: str,
                             reference_id: str, description: str, idempotency_key: str) -> tuple:
    """
    Charge a fee priced in `currency` (e.g. USD): from that balance if it covers it, otherwise from
    another balance at the platform rate (spread included). Returns (currency_charged, amount_charged).
    Raises ledger.InsufficientFunds if no single balance can cover it.
    """
    from app.models.wallet import WalletBalance
    from app.routers.wallet import _get_exchange_rate
    need = _money(amount)
    if need <= 0:
        return currency, Decimal(0)
    balances = db.query(WalletBalance).filter(WalletBalance.user_id == user_id).all()
    order = sorted(balances, key=lambda b: b.currency != currency.upper())  # fee currency first
    for b in order:
        cur = b.currency.upper()
        try:
            charge = need if cur == currency.upper() else _money(need * _d(_get_exchange_rate(db, currency.upper(), cur)))
        except Exception:  # no rate configured for that pair
            continue
        if _d(b.amount) >= charge:
            collect(db, user_id=user_id, currency=cur, amount=charge, from_bucket="available",
                    reference_type=reference_type, reference_id=reference_id,
                    description=description, idempotency_key=idempotency_key)
            return cur, charge
    raise ledger.InsufficientFunds(user_id, currency.upper(), "available", Decimal(0), need)
