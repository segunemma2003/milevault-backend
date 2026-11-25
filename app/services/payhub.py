"""
PayHub client — the central payment gateway (payhub.compasse.net) that owns the
Paystack/Flutterwave accounts and webhooks for all products. Bondrova is one "company"
in PayHub; we call it with Bondrova's API key and receive PayHub's signed webhooks.

Charges and payouts carry our own ids (client_reference) so retries are idempotent on
PayHub's side too, and bank accounts carry the Bondrova user id (external_ref).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings


class PayHubError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.PAYHUB_API_KEY)


def _request(method: str, path: str, *, json: Optional[dict] = None, params: Optional[dict] = None) -> Any:
    if not enabled():
        raise PayHubError("PayHub is not configured (PAYHUB_API_KEY missing).")
    url = settings.PAYHUB_BASE_URL.rstrip("/") + path
    with httpx.Client(timeout=20) as client:
        resp = client.request(method, url, json=json, params=params,
                              headers={"Authorization": f"Bearer {settings.PAYHUB_API_KEY}"})
    try:
        body = resp.json()
    except ValueError:
        raise PayHubError(f"PayHub returned HTTP {resp.status_code}")
    if resp.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else None
        raise PayHubError(detail if isinstance(detail, str) else f"PayHub error (HTTP {resp.status_code})")
    return body


# ─── Charges (deposits) ───────────────────────────────────────────────────────

def fee_quote(amount) -> Dict[str, Decimal]:
    """What to charge so Bondrova still receives `amount` after PayHub's service fee."""
    q = _request("GET", "/charges/fee-quote", params={"amount": str(amount)})
    return {"amount": Decimal(q["amount"]), "fee": Decimal(q["fee"]), "total": Decimal(q["total"])}


def create_charge(*, amount, customer_email: str, client_reference: str, redirect_url: str,
                  description: str, metadata: Optional[dict] = None) -> Dict[str, Any]:
    md = dict(metadata or {})
    md["redirect_url"] = redirect_url
    # Pin the provider: deposits must land in the same provider balance that payouts are paid from.
    c = _request("POST", "/charges", json={
        "provider": settings.PAYHUB_PROVIDER,
        "amount": str(amount), "customer_email": customer_email, "description": description,
        "client_reference": client_reference, "metadata": md,
    })
    return {"reference": c["reference"], "checkout_url": c.get("checkout_url"), "status": c["status"],
            "amount": Decimal(str(c["amount"])), "currency": c.get("currency", "NGN")}


def get_charge(reference: str) -> Dict[str, Any]:
    """Source of truth for a charge — webhook bodies are only hints."""
    c = _request("GET", f"/charges/{reference}")
    return {"reference": c["reference"], "status": c["status"], "amount": Decimal(str(c["amount"])),
            "currency": c.get("currency", "NGN"), "client_reference": c.get("client_reference")}


# ─── Bank accounts & payouts ──────────────────────────────────────────────────

def list_banks() -> List[Dict[str, str]]:
    banks = _request("GET", "/bank-accounts/banks", params={"provider": settings.PAYHUB_PROVIDER})
    return [{"name": b["name"], "code": b["code"]} for b in banks]


def register_bank_account(*, bank_code: str, bank_name: str, account_number: str, external_ref: str) -> Dict[str, Any]:
    """PayHub resolves the account with the bank and returns the bank's own account name."""
    a = _request("POST", "/bank-accounts", json={
        "provider": settings.PAYHUB_PROVIDER, "bank_code": bank_code, "bank_name": bank_name,
        "account_number": account_number, "external_ref": external_ref,
    })
    return {"id": a["id"], "status": a["status"], "account_name": a.get("account_name"),
            "error": a.get("verification_error")}


def request_payout(*, bank_account_id: str, amount, client_reference: str) -> Dict[str, Any]:
    p = _request("POST", "/payouts", json={"bank_account_id": bank_account_id, "amount": str(amount),
                                           "client_reference": client_reference})
    return {"id": p["id"], "status": p["status"], "failure_reason": p.get("failure_reason")}


def get_payout(payout_id: str) -> Dict[str, Any]:
    p = _request("GET", f"/payouts/{payout_id}")
    return {"id": p["id"], "status": p["status"], "client_reference": p.get("client_reference"),
            "failure_reason": p.get("failure_reason"), "amount": Decimal(str(p["amount"]))}


PAYOUT_DONE = {"completed"}
PAYOUT_FAILED = {"failed", "rejected"}


# ─── Webhooks ─────────────────────────────────────────────────────────────────

def verify_webhook(raw_body: bytes, signature_header: Optional[str], tolerance_seconds: int = 300) -> bool:
    """X-PayHub-Signature: t=<unix>,v1=HMAC-SHA256(secret, f"{t}.{body}")."""
    secret = settings.PAYHUB_WEBHOOK_SECRET
    if not secret or not signature_header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(","))
        t = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - t) > tolerance_seconds:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


def set_webhook(url: str) -> Dict[str, Any]:
    """Register Bondrova's webhook URL with PayHub. Returns the secret only the first time."""
    return _request("PUT", "/companies/me/webhook", json={"url": url})
