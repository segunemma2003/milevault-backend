"""
Multi-gateway payment service.
Gateway routing:
  - Paystack  → NG, GH, KE, ZA, EG (and any country the admin configures)
  - Stripe    → US, GB, EU zone (DE, FR, IT, ES, NL, BE, PT, AT, IE, SE, DK, FI, NO, CH)
  - Flutterwave → Africa-wide fallback + global
  - Coinbase Commerce → crypto payments (any country)
Admin can override per-country routing via PaymentGateway DB rows.
"""
import hmac
import hashlib
import json
import uuid
import httpx
from typing import Optional, Dict, Any, Tuple
from sqlalchemy.orm import Session

from app.config import settings
from app.models.currency import PaymentGateway, PaymentGatewayName

# ─── Country → default gateway mapping ───────────────────────────────────────

_PAYSTACK_COUNTRIES = {"NG", "GH", "KE", "ZA", "EG", "CI", "SN", "TZ", "UG", "RW"}

_STRIPE_COUNTRIES = {
    "US", "GB", "DE", "FR", "IT", "ES", "NL", "BE", "PT", "AT",
    "IE", "SE", "DK", "FI", "NO", "CH", "CA", "AU", "NZ", "SG", "JP",
}

_EU_CURRENCIES = {"EUR", "GBP", "CHF", "SEK", "DKK", "NOK"}


def get_gateway_for_country(
    country_code: str,
    currency: str,
    db: Session,
) -> PaymentGatewayName:
    """
    Queries admin-configured PaymentGateway rows first.
    Falls back to hardcoded defaults.
    """
    upper_country = (country_code or "").upper()
    upper_currency = (currency or "").upper()

    # Check crypto currencies first
    crypto_currencies = {"BTC", "ETH", "USDT", "USDC", "BNB", "SOL", "LTC"}
    if upper_currency in crypto_currencies:
        gw = db.query(PaymentGateway).filter(
            PaymentGateway.name == PaymentGatewayName.coinbase,
            PaymentGateway.is_active == True,
        ).first()
        if gw:
            return PaymentGatewayName.coinbase

    # Admin-configured gateways (wildcard "*" or specific country code)
    gateways = (
        db.query(PaymentGateway)
        .filter(PaymentGateway.is_active == True)
        .order_by(PaymentGateway.priority.asc())
        .all()
    )

    for gw in gateways:
        country_codes = gw.country_codes or []
        supported_currencies = gw.supported_currencies or []

        country_match = "*" in country_codes or upper_country in country_codes
        currency_match = not supported_currencies or upper_currency in supported_currencies

        if country_match and currency_match:
            return gw.name

    # Hardcoded fallback
    if upper_country in _PAYSTACK_COUNTRIES:
        return PaymentGatewayName.paystack
    if upper_country in _STRIPE_COUNTRIES:
        return PaymentGatewayName.stripe
    return PaymentGatewayName.flutterwave


# ─── Paystack ─────────────────────────────────────────────────────────────────

class PaystackError(Exception):
    pass


def paystack_initiate_deposit(
    amount_kobo: int,
    currency: str,
    email: str,
    reference: str,
    callback_url: str,
    metadata: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    amount_kobo: amount in smallest currency unit (kobo for NGN, pesewas for GHS, etc.)
    Returns Paystack authorization_url + access_code + reference.
    """
    if not settings.PAYSTACK_SECRET_KEY:
        raise PaystackError("Paystack secret key not configured.")

    payload = {
        "email": email,
        "amount": amount_kobo,
        "currency": currency.upper(),
        "reference": reference,
        "callback_url": callback_url,
        "metadata": metadata or {},
    }

    with httpx.Client(timeout=10) as client:
        resp = client.post(
            "https://api.paystack.co/transaction/initialize",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
                "Content-Type": "application/json",
            },
        )

    data = resp.json()
    if not data.get("status"):
        raise PaystackError(data.get("message", "Paystack initialization failed."))

    return {
        "gateway": "paystack",
        "authorization_url": data["data"]["authorization_url"],
        "access_code": data["data"]["access_code"],
        "reference": data["data"]["reference"],
    }


def paystack_verify_transaction(reference: str) -> Dict[str, Any]:
    """Verify a Paystack transaction by reference."""
    if not settings.PAYSTACK_SECRET_KEY:
        raise PaystackError("Paystack secret key not configured.")

    with httpx.Client(timeout=10) as client:
        resp = client.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
        )

    data = resp.json()
    if not data.get("status"):
        raise PaystackError(data.get("message", "Paystack verification failed."))

    tx = data["data"]
    return {
        "status": tx["status"],           # "success" | "failed" | "pending"
        "amount": tx["amount"] / 100,     # convert back from kobo
        "currency": tx["currency"],
        "reference": tx["reference"],
        "gateway_response": tx.get("gateway_response"),
        "paid_at": tx.get("paid_at"),
        "channel": tx.get("channel"),
    }


def paystack_verify_webhook(payload_bytes: bytes, signature: str) -> bool:
    """HMAC-SHA512 webhook verification."""
    expected = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode(),
        payload_bytes,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def paystack_initiate_transfer(
    amount_kobo: int,
    currency: str,
    recipient_code: str,
    reason: str,
    reference: str,
) -> Dict[str, Any]:
    """Initiate a payout to a recipient code (must be created via /transferrecipient first)."""
    if not settings.PAYSTACK_SECRET_KEY:
        raise PaystackError("Paystack secret key not configured.")

    payload = {
        "source": "balance",
        "amount": amount_kobo,
        "currency": currency.upper(),
        "recipient": recipient_code,
        "reason": reason,
        "reference": reference,
    }

    with httpx.Client(timeout=10) as client:
        resp = client.post(
            "https://api.paystack.co/transfer",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
                "Content-Type": "application/json",
            },
        )

    data = resp.json()
    if not data.get("status"):
        raise PaystackError(data.get("message", "Paystack transfer failed."))

    return {
        "gateway": "paystack",
        "transfer_code": data["data"]["transfer_code"],
        "reference": data["data"]["reference"],
        "status": data["data"]["status"],
    }


# ─── Stripe ───────────────────────────────────────────────────────────────────

class StripeError(Exception):
    pass


def stripe_initiate_deposit(
    amount_cents: int,
    currency: str,
    user_email: str,
    reference: str,
    return_url: str,
    metadata: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Creates a Stripe Payment Intent and returns client_secret."""
    if not settings.STRIPE_SECRET_KEY:
        raise StripeError("Stripe secret key not configured.")

    payload = {
        "amount": amount_cents,
        "currency": currency.lower(),
        "receipt_email": user_email,
        "metadata": {**(metadata or {}), "reference": reference},
        "automatic_payment_methods[enabled]": "true",
    }

    with httpx.Client(timeout=10) as client:
        resp = client.post(
            "https://api.stripe.com/v1/payment_intents",
            data=payload,
            auth=(settings.STRIPE_SECRET_KEY, ""),
        )

    data = resp.json()
    if "error" in data:
        raise StripeError(data["error"].get("message", "Stripe initiation failed."))

    return {
        "gateway": "stripe",
        "payment_intent_id": data["id"],
        "client_secret": data["client_secret"],
        "reference": reference,
        "status": data["status"],
    }


def stripe_verify_payment_intent(payment_intent_id: str) -> Dict[str, Any]:
    if not settings.STRIPE_SECRET_KEY:
        raise StripeError("Stripe secret key not configured.")

    with httpx.Client(timeout=10) as client:
        resp = client.get(
            f"https://api.stripe.com/v1/payment_intents/{payment_intent_id}",
            auth=(settings.STRIPE_SECRET_KEY, ""),
        )

    data = resp.json()
    if "error" in data:
        raise StripeError(data["error"].get("message", "Stripe verification failed."))

    return {
        "status": data["status"],          # "succeeded" | "requires_payment_method" | etc.
        "amount": data["amount"] / 100,
        "currency": data["currency"].upper(),
        "payment_intent_id": data["id"],
    }


def stripe_verify_webhook(payload_bytes: bytes, sig_header: str) -> Dict[str, Any]:
    """Stripe webhook signature verification using webhook secret."""
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise StripeError("Stripe webhook secret not configured.")

    try:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        event = stripe.Webhook.construct_event(
            payload_bytes, sig_header, settings.STRIPE_WEBHOOK_SECRET
        )
        return event
    except Exception as e:
        raise StripeError(f"Stripe webhook verification failed: {e}")


def stripe_initiate_payout(
    amount_cents: int,
    currency: str,
    destination: str,
    reference: str,
) -> Dict[str, Any]:
    """Payout to a connected Stripe account or bank account."""
    if not settings.STRIPE_SECRET_KEY:
        raise StripeError("Stripe secret key not configured.")

    with httpx.Client(timeout=10) as client:
        resp = client.post(
            "https://api.stripe.com/v1/payouts",
            data={
                "amount": amount_cents,
                "currency": currency.lower(),
                "destination": destination,
                "metadata[reference]": reference,
            },
            auth=(settings.STRIPE_SECRET_KEY, ""),
        )

    data = resp.json()
    if "error" in data:
        raise StripeError(data["error"].get("message", "Stripe payout failed."))

    return {
        "gateway": "stripe",
        "payout_id": data["id"],
        "status": data["status"],
        "reference": reference,
    }


# ─── Flutterwave ──────────────────────────────────────────────────────────────

class FlutterwaveError(Exception):
    pass


def flutterwave_initiate_deposit(
    amount: float,
    currency: str,
    user_email: str,
    user_name: str,
    reference: str,
    redirect_url: str,
    metadata: Optional[Dict] = None,
) -> Dict[str, Any]:
    if not settings.FLUTTERWAVE_SECRET_KEY:
        raise FlutterwaveError("Flutterwave secret key not configured.")

    payload = {
        "tx_ref": reference,
        "amount": amount,
        "currency": currency.upper(),
        "redirect_url": redirect_url,
        "customer": {
            "email": user_email,
            "name": user_name,
        },
        "meta": metadata or {},
        "customizations": {
            "title": "MileVault",
            "description": "Secure escrow deposit",
        },
    }

    with httpx.Client(timeout=10) as client:
        resp = client.post(
            "https://api.flutterwave.com/v3/payments",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET_KEY}",
                "Content-Type": "application/json",
            },
        )

    data = resp.json()
    if data.get("status") != "success":
        raise FlutterwaveError(data.get("message", "Flutterwave initiation failed."))

    return {
        "gateway": "flutterwave",
        "payment_link": data["data"]["link"],
        "reference": reference,
    }


def flutterwave_verify_transaction(transaction_id: str) -> Dict[str, Any]:
    if not settings.FLUTTERWAVE_SECRET_KEY:
        raise FlutterwaveError("Flutterwave secret key not configured.")

    with httpx.Client(timeout=10) as client:
        resp = client.get(
            f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify",
            headers={"Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET_KEY}"},
        )

    data = resp.json()
    if data.get("status") != "success":
        raise FlutterwaveError(data.get("message", "Flutterwave verification failed."))

    tx = data["data"]
    return {
        "status": tx["status"],           # "successful" | "failed"
        "amount": tx["amount"],
        "currency": tx["currency"],
        "reference": tx["tx_ref"],
        "transaction_id": tx["id"],
    }


def flutterwave_verify_webhook(payload_bytes: bytes, signature: str) -> bool:
    """Flutterwave webhook verification using secret hash."""
    secret_hash = settings.FLUTTERWAVE_WEBHOOK_SECRET
    if not secret_hash:
        return False
    expected = hmac.new(
        secret_hash.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def flutterwave_initiate_transfer(
    amount: float,
    currency: str,
    account_number: str,
    account_bank: str,
    narration: str,
    reference: str,
) -> Dict[str, Any]:
    if not settings.FLUTTERWAVE_SECRET_KEY:
        raise FlutterwaveError("Flutterwave secret key not configured.")

    payload = {
        "account_bank": account_bank,
        "account_number": account_number,
        "amount": amount,
        "currency": currency.upper(),
        "narration": narration,
        "reference": reference,
    }

    with httpx.Client(timeout=10) as client:
        resp = client.post(
            "https://api.flutterwave.com/v3/transfers",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET_KEY}",
                "Content-Type": "application/json",
            },
        )

    data = resp.json()
    if data.get("status") != "success":
        raise FlutterwaveError(data.get("message", "Flutterwave transfer failed."))

    return {
        "gateway": "flutterwave",
        "transfer_id": data["data"]["id"],
        "status": data["data"]["status"],
        "reference": reference,
    }


# ─── Unified interface ────────────────────────────────────────────────────────

def generate_payment_reference(prefix: str = "MVL") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"


def initiate_deposit(
    *,
    user,
    amount: float,
    currency: str,
    callback_url: str,
    db: Session,
    metadata: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Routes deposit initiation to the correct gateway based on user's country.
    Returns a unified response with `gateway`, `redirect_url`/`client_secret`, and `reference`.
    """
    gateway = get_gateway_for_country(user.country_code or "", currency, db)
    reference = generate_payment_reference()

    if gateway == PaymentGatewayName.paystack:
        # Paystack amounts are in kobo (×100 for NGN, same for GHS pesewas, etc.)
        amount_minor = int(amount * 100)
        return paystack_initiate_deposit(
            amount_kobo=amount_minor,
            currency=currency,
            email=user.email,
            reference=reference,
            callback_url=callback_url,
            metadata=metadata,
        )

    if gateway == PaymentGatewayName.stripe:
        amount_cents = int(amount * 100)
        return stripe_initiate_deposit(
            amount_cents=amount_cents,
            currency=currency,
            user_email=user.email,
            reference=reference,
            return_url=callback_url,
            metadata=metadata,
        )

    if gateway == PaymentGatewayName.flutterwave:
        return flutterwave_initiate_deposit(
            amount=amount,
            currency=currency,
            user_email=user.email,
            user_name=f"{user.first_name} {user.last_name}".strip(),
            reference=reference,
            redirect_url=callback_url,
            metadata=metadata,
        )

    # Manual / unsupported gateway
    return {
        "gateway": "manual",
        "reference": reference,
        "message": "Manual payment — please contact support to complete this deposit.",
    }


def verify_deposit(
    *,
    gateway: str,
    reference: str,
    db: Session,
) -> Dict[str, Any]:
    """Verify a deposit from any gateway. Returns unified status dict."""
    if gateway == "paystack":
        result = paystack_verify_transaction(reference)
        return {
            "success": result["status"] == "success",
            "amount": result["amount"],
            "currency": result["currency"],
            "reference": result["reference"],
            "gateway": "paystack",
        }

    if gateway == "stripe":
        result = stripe_verify_payment_intent(reference)
        return {
            "success": result["status"] == "succeeded",
            "amount": result["amount"],
            "currency": result["currency"],
            "reference": result["payment_intent_id"],
            "gateway": "stripe",
        }

    if gateway == "flutterwave":
        result = flutterwave_verify_transaction(reference)
        return {
            "success": result["status"] == "successful",
            "amount": result["amount"],
            "currency": result["currency"],
            "reference": result["reference"],
            "gateway": "flutterwave",
        }

    raise ValueError(f"Unknown gateway: {gateway}")
