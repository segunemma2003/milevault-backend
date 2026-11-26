"""
Deal documents: the contract (generated from the deal's terms, signed by both parties) and the
payment receipt (branded with the buyer's logo, three templates). Both are rendered as printable
HTML pages — "Download PDF" uses the browser's print-to-PDF, so the server needs no PDF engine.
"""
from __future__ import annotations

import hashlib
import html
import json
import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.transaction import DealContract, IssuedDocument, Milestone, Transaction
from app.models.user import User
from app.models.wallet import WalletTransaction

RECEIPT_TEMPLATES = ("classic", "modern", "minimal")
SIGNABLE_EXCLUDED = ("cancelled", "declined", "expired", "refunded")


class DocumentError(Exception):
    def __init__(self, status_code: int, error: str, message: str):
        super().__init__(message)
        self.status_code, self.error, self.message = status_code, error, message


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _money(currency: str, amount) -> str:
    try:
        return f"{currency} {float(amount or 0):,.2f}"
    except (TypeError, ValueError):
        return f"{currency} {amount}"


def _date(d: Optional[datetime]) -> str:
    return d.strftime("%d %b %Y") if d else "—"


def _datetime(d: Optional[datetime]) -> str:
    return d.strftime("%d %b %Y, %H:%M UTC") if d else "—"


def _name(u: Optional[User]) -> str:
    return f"{u.first_name} {u.last_name}".strip() if u else "—"


def _milestones(db: Session, tx: Transaction) -> list[Milestone]:
    return db.query(Milestone).filter(Milestone.transaction_id == tx.id).order_by(
        Milestone.created_at.asc(), Milestone.id.asc()).all()


# ─── Contract ────────────────────────────────────────────────────────────────

def build_terms(db: Session, tx: Transaction) -> dict:
    """Everything both parties agree to. Changing any of it creates a new contract version."""
    from app.services.fees import platform_settings
    from app.services.platform_timeline import get_auto_release_days, get_funding_deadline_days

    s = platform_settings(db)
    agent = None
    req = getattr(tx, "agent_request", None)
    if req is not None and req.agent is not None and str(getattr(req.status, "value", req.status)) not in ("cancelled", "declined"):
        agent = {"name": _name(req.agent.user), "fee": float(req.fee_charged or 0),
                 "currency": (req.fee_currency or tx.currency or "USD").upper()}
    return {
        "deal_id": tx.id,
        "title": tx.title,
        "description": tx.description or "",
        "currency": tx.currency,
        "amount": round(float(tx.amount or 0), 2),
        "buyer": {"id": tx.buyer_id, "name": _name(tx.buyer), "email": tx.buyer.email if tx.buyer else None},
        "seller": {"id": tx.seller_id, "name": _name(tx.seller), "email": tx.seller.email if tx.seller else None},
        "agent": agent,
        "expected_completion_date": tx.expected_completion_date.date().isoformat() if tx.expected_completion_date else None,
        "milestones": [
            {
                "title": m.title,
                "description": m.description or "",
                "expectations": m.expectations or "",
                "amount": round(float(m.amount or 0), 2),
                "buyer_fee": round(float(m.buyer_fee or 0), 2),
                "seller_fee": round(float(m.seller_fee or 0), 2),
                "due_date": m.due_date.date().isoformat() if m.due_date else None,
            }
            for m in _milestones(db, tx)
        ],
        "fee_paid_by": tx.service_fee_payment or "buyer",
        "review_days": get_auto_release_days(db),
        "funding_deadline_days": get_funding_deadline_days(db),
        "dispute_fee": {"fixed": float(s.dispute_fee_fixed or 0), "percent": float(s.dispute_fee_percent or 0)},
    }


# Platform-wide rules quoted in the contract. They're fixed when a version is created, so an admin
# changing a setting later doesn't silently reset every deal's signatures.
PLATFORM_KEYS = ("review_days", "funding_deadline_days", "dispute_fee")


def terms_hash(terms: dict) -> str:
    deal_terms = {k: v for k, v in terms.items() if k not in PLATFORM_KEYS}
    return hashlib.sha256(json.dumps(deal_terms, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _require_party(tx: Transaction, user: User) -> str:
    if user.id == tx.buyer_id:
        return "buyer"
    if user.id == tx.seller_id:
        return "seller"
    raise DocumentError(403, "ACCESS_DENIED", "Only the buyer or the seller of this deal can do this.")


def current_contract(db: Session, tx: Transaction) -> Optional[DealContract]:
    """The contract for the deal's CURRENT terms — a new version is made if the terms changed."""
    if not tx.buyer_id or not tx.seller_id:
        return None
    latest = (db.query(DealContract).filter(DealContract.transaction_id == tx.id)
              .order_by(DealContract.version.desc()).first())
    if latest and (tx.status == "completed" or tx.status in SIGNABLE_EXCLUDED):
        return latest  # a finished deal keeps the contract it had
    terms = build_terms(db, tx)
    h = terms_hash(terms)
    if latest and latest.terms_hash == h:
        return latest
    if latest:
        latest.status = "superseded"
    c = DealContract(transaction_id=tx.id, version=(latest.version + 1) if latest else 1,
                     terms=terms, terms_hash=h, status="awaiting_signatures")
    db.add(c)
    tx.contract_signed = False
    db.flush()
    return c


def sign(db: Session, tx: Transaction, user: User, full_name: str, ip: Optional[str]) -> DealContract:
    side = _require_party(tx, user)
    if tx.status in SIGNABLE_EXCLUDED:
        raise DocumentError(409, "DEAL_CLOSED", f"This deal is {tx.status}; its contract can't be signed.")
    c = current_contract(db, tx)
    if c is None:
        raise DocumentError(409, "PARTY_MISSING", "Both parties must be on the deal before the contract can be signed.")
    typed = " ".join((full_name or "").split())
    expected = " ".join(_name(user).split())
    if typed.lower() != expected.lower():
        raise DocumentError(422, "NAME_MISMATCH", f"Type your full name exactly as on your account ({expected}) to sign.")
    if getattr(c, f"{side}_signed_at"):
        raise DocumentError(409, "ALREADY_SIGNED", "You have already signed this version of the contract.")
    setattr(c, f"{side}_signed_name", typed)
    setattr(c, f"{side}_signed_at", datetime.utcnow())
    setattr(c, f"{side}_signed_ip", (ip or "")[:64] or None)
    if c.buyer_signed_at and c.seller_signed_at:
        c.status = "signed"
        tx.contract_signed = True
    db.flush()
    return c


def contract_to_dict(c: Optional[DealContract], viewer: Optional[User] = None) -> Optional[dict]:
    if not c:
        return None
    return {
        "id": c.id, "version": c.version, "status": c.status, "terms_hash": c.terms_hash,
        "terms": c.terms,
        "buyer_signed_at": c.buyer_signed_at.isoformat() if c.buyer_signed_at else None,
        "buyer_signed_name": c.buyer_signed_name,
        "seller_signed_at": c.seller_signed_at.isoformat() if c.seller_signed_at else None,
        "seller_signed_name": c.seller_signed_name,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


_PAGE_CSS = """
*{box-sizing:border-box} body{margin:0;background:#f3f4f6;color:#111827;font:15px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.sheet{max-width:820px;margin:24px auto;background:#fff;padding:48px 56px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
h1{font-size:24px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px;text-transform:uppercase;letter-spacing:.04em;color:#374151}
table{width:100%;border-collapse:collapse;margin:8px 0} th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top}
th{font-size:12px;text-transform:uppercase;color:#6b7280;letter-spacing:.04em} .num{text-align:right;white-space:nowrap}
.muted{color:#6b7280;font-size:13px} .toolbar{max-width:820px;margin:16px auto 0;display:flex;gap:8px;justify-content:flex-end;padding:0 16px}
.toolbar button{font:inherit;padding:8px 16px;border-radius:6px;border:1px solid #d1d5db;background:#fff;cursor:pointer}
.toolbar button.primary{background:#111827;color:#fff;border-color:#111827}
.sig{display:flex;gap:24px;flex-wrap:wrap;margin-top:12px} .sig>div{flex:1;min-width:220px;border:1px solid #e5e7eb;border-radius:8px;padding:12px 16px}
.sig .name{font-family:"Brush Script MT",cursive;font-size:26px;min-height:36px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
.ok{background:#dcfce7;color:#166534} .wait{background:#fef3c7;color:#92400e} .old{background:#e5e7eb;color:#374151}
ol li{margin-bottom:6px}
.verify{display:flex;gap:16px;align-items:center;margin-top:28px;padding:14px 16px;border:1px dashed #9ca3af;border-radius:8px;font-size:13px} .verify svg{width:96px;height:96px;flex-shrink:0} .code{font-family:ui-monospace,Menlo,monospace;font-weight:700;letter-spacing:.04em}
.notice{background:#fef3c7;color:#92400e;padding:10px 14px;border-radius:8px;margin:12px 0;font-size:14px}
@media (max-width:640px){.sheet{margin:0;padding:24px 16px;box-shadow:none}}
@media print{body{background:#fff}.toolbar{display:none}.sheet{margin:0;box-shadow:none;padding:0}}
"""

_TOOLBAR = ('<div class="toolbar"><button onclick="window.close()">Close</button>'
            '<button class="primary" onclick="window.print()">Download PDF / Print</button></div>')


def _page(title: str, body: str, extra_css: str = "") -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{_e(title)}</title>"
            f"<style>{_PAGE_CSS}{extra_css}</style></head><body>{_TOOLBAR}<div class='sheet'>{body}</div></body></html>")


def render_contract(db: Session, c: DealContract) -> str:
    t = c.terms
    doc = issue_contract(db, c)
    cur = t["currency"]
    status = {"signed": ("Signed by both parties", "ok"), "superseded": ("Superseded by a newer version", "old")}.get(
        c.status, ("Awaiting signatures", "wait"))
    rows = "".join(
        f"<tr><td>{i}</td><td><strong>{_e(m['title'])}</strong>"
        + (f"<div class='muted'>{_e(m['description'])}</div>" if m["description"] else "")
        + (f"<div class='muted'>Acceptance: {_e(m['expectations'])}</div>" if m["expectations"] else "")
        + f"</td><td>{_e(m['due_date'] or '—')}</td><td class='num'>{_money(cur, m['amount'])}</td></tr>"
        for i, m in enumerate(t["milestones"], 1))
    payer = {"buyer": "the Buyer", "seller": "the Seller", "split": "both parties in the agreed split"}.get(
        t["fee_paid_by"], "the Buyer")
    df = t.get("dispute_fee") or {}
    dispute_fee = " + ".join(x for x in (
        _money("USD", df["fixed"]) if df.get("fixed") else "", f"{df['percent']:g}% of the disputed amount" if df.get("percent") else "") if x) or "no fee"
    agent = t.get("agent")
    agent_clause = (f"<li><strong>Verification agent.</strong> {_e(agent['name'])} acts as the verification agent for a fee of "
                    f"{_money(agent['currency'], agent['fee'])}, paid by the Buyer and held by Bondrova. The fee is paid to "
                    f"the agent only once their verification is complete; otherwise it is returned to the Buyer.</li>") if agent else ""

    def sig(side: str, label: str) -> str:
        at = getattr(c, f"{side}_signed_at")
        name = getattr(c, f"{side}_signed_name")
        return (f"<div><div class='muted'>{label}: {_e(t[side]['name'])}</div><div class='name'>{_e(name) if at else ''}</div>"
                f"<div class='muted'>{('Signed ' + _datetime(at)) if at else 'Not signed yet'}</div></div>")

    body = f"""
<div style="display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;align-items:flex-start">
  <div><h1>Escrow Agreement</h1><div class="muted">{_e(t['title'])} · Deal {_e(t['deal_id'][:8].upper())} · Version {c.version}</div></div>
  <span class="badge {status[1]}">{status[0]}</span>
</div>
<h2>1. Parties</h2>
<table><tr><th>Buyer</th><th>Seller</th></tr>
<tr><td>{_e(t['buyer']['name'])}<div class="muted">{_e(t['buyer']['email'])}</div></td>
<td>{_e(t['seller']['name'])}<div class="muted">{_e(t['seller']['email'])}</div></td></tr></table>
<p class="muted">Bondrova holds the payment in escrow and releases it according to this agreement. Bondrova is not a party to the sale itself.</p>
<h2>2. What is being delivered</h2>
<p><strong>{_e(t['title'])}</strong></p>{f"<p>{_e(t['description'])}</p>" if t['description'] else ''}
{f"<p class='muted'>Expected completion: {_e(t['expected_completion_date'])}</p>" if t.get('expected_completion_date') else ''}
<table><tr><th>#</th><th>Milestone</th><th>Due</th><th class="num">Price</th></tr>{rows}
<tr><td></td><td><strong>Total</strong></td><td></td><td class="num"><strong>{_money(cur, t['amount'])}</strong></td></tr></table>
<h2>3. Terms</h2>
<ol>
<li><strong>Payment into escrow.</strong> The Buyer pays each milestone into Bondrova escrow. Work on a milestone starts only once it is fully paid. If no milestone is fully paid within {t['funding_deadline_days']} days of the deal being accepted, the deal is cancelled and any part-payment is returned to the Buyer.</li>
<li><strong>Escrow fee.</strong> Bondrova's escrow fee is paid by {payer}, as shown on each milestone when the deal was created.</li>
<li><strong>Delivery and release.</strong> The Seller submits each milestone with proof of delivery. The money for that milestone is released to the Seller when the Buyer approves it, when the Seller enters the Buyer's handover code, or automatically {t['review_days']} days after delivery if the Buyer neither approves nor asks for changes.</li>
<li><strong>Changes.</strong> The Buyer may ask for revisions to a delivered milestone. Any change to these terms creates a new version of this agreement that both parties must sign again.</li>
<li><strong>Disputes.</strong> Either party may raise a dispute before a milestone is released. Bondrova reviews the evidence and its decision on the escrowed money is final for this deal. Dispute fee: {dispute_fee}, paid by the party who raises it.</li>
<li><strong>Cancellation.</strong> Once the deal has been accepted, it can be cancelled only if both parties agree. On cancellation, money still in escrow is returned to the Buyer; milestones already released stay paid to the Seller.</li>
{agent_clause}
<li><strong>Electronic signature.</strong> Each party signs by typing their full name. The signature, its time and the network address it came from are recorded and bound to these exact terms (reference below).</li>
</ol>
<h2>4. Signatures</h2>
<div class="sig">{sig('buyer', 'Buyer')}{sig('seller', 'Seller')}</div>
<p class="muted" style="margin-top:24px">Agreement reference (SHA-256 of the terms): {_e(c.terms_hash)}<br>Generated {_datetime(c.created_at)} by Bondrova.</p>
{verify_block(doc)}
"""
    return _page(f"Escrow Agreement — {t['title']}", body)


# ─── Verification (public codes + QR) ────────────────────────────────────────

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L — easy to read out loud


def _short(u: Optional[User]) -> str:
    """Public form of a name: first name + last initial."""
    if not u:
        return "—"
    last = (u.last_name or "").strip()
    return f"{(u.first_name or '').strip()} {last[:1] + '.' if last else ''}".strip()


def _hash(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum() or ch == "-").strip("-")


def verify_url(code: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/verify/{code}"


def _issue(db: Session, tx: Transaction, kind: str, snapshot: dict) -> IssuedDocument:
    """The code for exactly these figures — reused if already issued, otherwise a new one."""
    h = _hash(snapshot)
    doc = (db.query(IssuedDocument)
           .filter(IssuedDocument.transaction_id == tx.id, IssuedDocument.kind == kind, IssuedDocument.content_hash == h)
           .order_by(IssuedDocument.issued_at.asc()).first())
    if doc:
        return doc
    year = datetime.utcnow().year
    for _ in range(8):
        code = f"BDR-{year}-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        if not db.query(IssuedDocument.id).filter(IssuedDocument.code == code).first():
            break
    doc = IssuedDocument(code=code, kind=kind, transaction_id=tx.id, snapshot=snapshot, content_hash=h)
    db.add(doc)
    db.flush()
    return doc


def qr_svg(url: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=1)
        out = img.to_string()
        return out.decode() if isinstance(out, bytes) else out
    except Exception:  # noqa: BLE001 — the code and link below still let anyone verify
        return ""


def verify_block(doc: IssuedDocument, with_qr: bool = True) -> str:
    url = verify_url(doc.code)
    qr = qr_svg(url) if with_qr else ""
    return (f"<div class='verify'>{qr}<div><strong>Verify this document</strong><br>"
            f"Code <span class='code'>{_e(doc.code)}</span> · issued {_datetime(doc.issued_at)}<br>"
            f"Scan the code or visit <a href='{_e(url)}'>{_e(url)}</a><br>"
            f"<span class='muted'>Bondrova keeps a copy of the figures on this document. If what you see here doesn't match "
            f"the verification page, the document has been altered.</span></div></div>")


def contract_snapshot(c: DealContract) -> dict:
    t = c.terms
    return {
        "deal_id": t["deal_id"], "title": t["title"], "currency": t["currency"], "amount": t["amount"],
        "version": c.version, "terms_hash": c.terms_hash, "status": c.status,
        "buyer": _short_name(t["buyer"]["name"]), "seller": _short_name(t["seller"]["name"]),
        "buyer_signed_at": c.buyer_signed_at.isoformat() if c.buyer_signed_at else None,
        "seller_signed_at": c.seller_signed_at.isoformat() if c.seller_signed_at else None,
        "milestones": [{"title": m["title"], "amount": m["amount"]} for m in t["milestones"]],
    }


def _short_name(full: str) -> str:
    parts = (full or "").split()
    return f"{parts[0]} {parts[-1][:1]}." if len(parts) > 1 else (parts[0] if parts else "—")


def issue_contract(db: Session, c: DealContract) -> IssuedDocument:
    tx = db.get(Transaction, c.transaction_id)
    return _issue(db, tx, "contract", contract_snapshot(c))


# ─── Receipt ─────────────────────────────────────────────────────────────────

_TEMPLATE_CSS = {
    "classic": ".rc-head{border-bottom:3px double #111827;padding-bottom:16px} .sheet{font-family:Georgia,'Times New Roman',serif}",
    "modern": (".rc-head{background:#0f766e;color:#fff;margin:-48px -56px 24px;padding:32px 56px} .rc-head .muted{color:#ccfbf1}"
               " th{color:#0f766e} .total td{background:#f0fdfa}"
               " @media (max-width:640px){.rc-head{margin:-24px -16px 16px;padding:24px 16px}}"),
    "minimal": ".rc-head{padding-bottom:8px} th,td{border-bottom:none;padding:6px 0} h2{color:#9ca3af;font-weight:500}",
}

_KIND = {"escrow_lock": "Paid into escrow", "escrow_release": "Released to seller",
         "escrow_refund": "Refunded to buyer", "refund": "Dispute refund"}


def receipt_rows(db: Session, tx: Transaction) -> list[WalletTransaction]:
    return (db.query(WalletTransaction)
            .filter(WalletTransaction.transaction_id == tx.id, WalletTransaction.type.in_(tuple(_KIND)),
                    WalletTransaction.status == "completed")
            .order_by(WalletTransaction.created_at.asc()).all())


def receipt_summary(db: Session, tx: Transaction) -> dict:
    """
    Where the buyer's money is, from Bondrova's records:
        paid into escrow = released to seller + Bondrova fees + refunded + still held
    "Still held" comes from the milestones themselves (funded but not released), so fees taken on
    release are never shown as money still in escrow.
    """
    from app.models.currency import Refund

    ms = _milestones(db, tx)
    rows = receipt_rows(db, tx)
    paid = sum(float(r.amount or 0) for r in rows if r.type == "escrow_lock")
    released = sum(float(r.amount or 0) for r in rows if r.type == "escrow_release")
    cancel_refunds = sum(float(r.amount or 0) for r in rows if r.type == "escrow_refund")
    dispute_refunds = sum(float(r.amount or 0) for r in db.query(Refund).filter(
        Refund.transaction_id == tx.id, Refund.status == "completed").all())
    fees = 0.0
    unreleased = 0.0
    for m in ms:
        funded = float(m.funded_amount or 0)
        if m.status == "completed":
            fees += min(float(m.buyer_fee or 0) + float(m.seller_fee or 0), funded or float(m.amount or 0))
        else:
            unreleased += funded
    held = max(0.0, unreleased - dispute_refunds)
    r2 = lambda v: round(v + 0.0, 2)  # noqa: E731
    return {
        "deal_id": tx.id, "title": tx.title, "status": tx.status, "currency": tx.currency,
        "deal_total": r2(float(tx.amount or 0)),
        "paid_into_escrow": r2(paid), "released_to_seller": r2(released), "bondrova_fees": r2(fees),
        "refunded_to_buyer": r2(cancel_refunds + dispute_refunds), "held_in_escrow": r2(held),
        "buyer": _short(tx.buyer), "seller": _short(tx.seller),
        "milestones": [{"title": m.title, "status": m.status, "amount": r2(float(m.amount or 0)),
                        "released": m.status == "completed"} for m in ms],
    }


def issue_receipt(db: Session, tx: Transaction) -> tuple[IssuedDocument, dict]:
    summary = receipt_summary(db, tx)
    return _issue(db, tx, "receipt", summary), summary


def render_receipt(db: Session, tx: Transaction, template: Optional[str]) -> str:
    body, css, title = _receipt_parts(db, tx, template)
    return _page(title, body, css)


def receipt_email_html(db: Session, tx: Transaction, template: Optional[str]) -> str:
    """Same receipt for an email body (no toolbar; styles travel with it; no QR — mail apps drop SVG)."""
    body, css, _ = _receipt_parts(db, tx, template, email=True)
    return f"<style>{_PAGE_CSS}{css}</style><div class='sheet'>{body}</div>"


def _receipt_parts(db: Session, tx: Transaction, template: Optional[str], email: bool = False) -> tuple[str, str, str]:
    buyer = tx.buyer
    template = template if template in RECEIPT_TEMPLATES else (getattr(buyer, "receipt_template", None) or "classic")
    if template not in RECEIPT_TEMPLATES:
        template = "classic"
    doc, sm = issue_receipt(db, tx)
    cur = tx.currency
    lines = "".join(
        f"<tr><td>{_datetime(r.created_at)}</td><td>{_KIND[r.type]}</td><td>{_e(r.description or '')}</td>"
        f"<td class='num'>{_money(r.currency, r.amount)}</td></tr>" for r in receipt_rows(db, tx))
    if not lines:
        lines = "<tr><td colspan='4' class='muted'>No payments on this deal yet.</td></tr>"
    logo = getattr(buyer, "business_logo_url", None)
    issuer = getattr(buyer, "business_name", None) or _name(buyer)
    logo_html = (f"<img src='{_e(logo)}' alt='' style='max-height:64px;max-width:200px;object-fit:contain'>" if logo else
                 f"<div style='font-size:20px;font-weight:700'>{_e(issuer)}</div>")
    ms_rows = "".join(
        f"<tr><td>{_e(m['title'])}</td><td>{'Released' if m['released'] else _e((m['status'] or '').replace('_', ' ').capitalize())}</td>"
        f"<td class='num'>{_money(cur, m['amount'])}</td></tr>" for m in sm["milestones"])
    held = sm["held_in_escrow"]
    if held > 0:
        notice = (f"<div class='notice'>{_money(cur, held)} is still held in Bondrova escrow. It has <strong>not</strong> been "
                  f"paid to the seller yet — it is released only when the buyer approves the work.</div>")
    elif sm["paid_into_escrow"] == 0:
        notice = "<div class='notice'>Nothing has been paid into escrow for this deal yet.</div>"
    else:
        notice = ""
    title = "Payment receipt" if sm["released_to_seller"] > 0 or sm["refunded_to_buyer"] > 0 else "Escrow statement"
    body = f"""
<div class="rc-head" style="display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;align-items:center">
  <div>{logo_html}<div class="muted">{_e(issuer)}{(' · ' + _e(buyer.email)) if buyer and buyer.email else ''}</div></div>
  <div style="text-align:right"><h1>{title}</h1><div class="muted">No. {_e(doc.code)} · {_date(doc.issued_at)}</div></div>
</div>
{notice}
<h2>Deal</h2>
<table><tr><th>Deal</th><th>Buyer</th><th>Seller</th><th>Status</th></tr>
<tr><td>{_e(tx.title)}</td><td>{_e(_name(tx.buyer))}</td><td>{_e(_name(tx.seller))}</td><td>{_e((tx.status or '').replace('_', ' ').capitalize())}</td></tr></table>
<h2>Milestones</h2>
<table><tr><th>Milestone</th><th>Status</th><th class="num">Price</th></tr>{ms_rows}
<tr class="total"><td><strong>Deal total</strong></td><td></td><td class="num"><strong>{_money(cur, sm['deal_total'])}</strong></td></tr></table>
<h2>Payments</h2>
<table><tr><th>Date</th><th>Type</th><th>Details</th><th class="num">Amount</th></tr>{lines}</table>
<table style="max-width:380px;margin-left:auto">
<tr><td>Paid into escrow</td><td class="num">{_money(cur, sm['paid_into_escrow'])}</td></tr>
<tr><td>Released to seller</td><td class="num">{_money(cur, sm['released_to_seller'])}</td></tr>
<tr><td>Bondrova fees</td><td class="num">{_money(cur, sm['bondrova_fees'])}</td></tr>
<tr><td>Refunded to buyer</td><td class="num">{_money(cur, sm['refunded_to_buyer'])}</td></tr>
<tr class="total"><td><strong>Still held in escrow</strong></td><td class="num"><strong>{_money(cur, held)}</strong></td></tr></table>
<p class="muted" style="margin-top:24px">Payments are held and released by Bondrova escrow. Amounts paid into escrow include the buyer's share of the escrow fee; amounts released to the seller are after the escrow fee.</p>
{verify_block(doc, with_qr=not email)}
"""
    return body, _TEMPLATE_CSS[template], f"{title} — {tx.title}"


# ─── Public verification ─────────────────────────────────────────────────────

def verify(db: Session, code: str) -> Optional[dict]:
    """What a verification page shows. Only public-safe fields: names as 'First L.', no emails."""
    doc = db.query(IssuedDocument).filter(IssuedDocument.code == normalize_code(code)).first()
    if not doc:
        return None
    tx = db.get(Transaction, doc.transaction_id)
    current, latest_code = False, None
    if tx is not None:
        if doc.kind == "receipt":
            now = receipt_summary(db, tx)
            current = _hash(now) == doc.content_hash
        else:
            latest = (db.query(DealContract).filter(DealContract.transaction_id == tx.id)
                      .order_by(DealContract.version.desc()).first())
            current = bool(latest) and _hash(contract_snapshot(latest)) == doc.content_hash
        if not current:
            newer = (db.query(IssuedDocument).filter(IssuedDocument.transaction_id == doc.transaction_id,
                                                     IssuedDocument.kind == doc.kind,
                                                     IssuedDocument.issued_at > doc.issued_at)
                     .order_by(IssuedDocument.issued_at.desc()).first())
            latest_code = newer.code if newer else None
    return {
        "valid": True, "code": doc.code, "kind": doc.kind,
        "issued_at": doc.issued_at.isoformat() + "Z" if doc.issued_at else None,
        "current": current,
        "status_message": ("These are the current figures for this deal." if current else
                           "This document is genuine, but the deal has changed since it was issued "
                           "(for example, a new payment or release). The figures below are as they were on the issue date."),
        "newer_code": latest_code,
        "deal_status_now": tx.status if tx else None,
        "snapshot": doc.snapshot,
    }


# ─── Automatic emails (deal completed / cancelled with a refund) ─────────────

_EMAIL_KEY = "bondrova_pending_receipts"


def queue_receipt_email(db: Session, tx: Transaction, reason: str) -> None:
    """Render the final receipt now (inside this transaction, so its figures are the final ones) and
    email it to both parties after the commit. A rollback sends nothing."""
    try:
        with db.begin_nested():
            page = receipt_email_html(db, tx, None)
            code = issue_receipt(db, tx)[0].code
    except Exception:  # noqa: BLE001 — never let a receipt break a payment
        import logging
        logging.getLogger("bondrova.documents").exception("receipt render failed for %s", tx.id)
        return
    pending = db.info.setdefault(_EMAIL_KEY, [])
    for u in (tx.buyer, tx.seller):
        if u is not None and u.email:
            intro = (f"<p>Hi {_e(u.first_name or 'there')},</p><p>{_e(reason)} Your receipt for <strong>{_e(tx.title)}</strong> "
                     f"is below. You can check it any time at <a href='{_e(verify_url(code))}'>{_e(verify_url(code))}</a>.</p>")
            pending.append((u.email, f"Receipt — {tx.title} ({code})", intro + page))


def _register_email_hooks() -> None:
    from sqlalchemy import event

    @event.listens_for(Session, "after_commit")
    def _send(session: Session) -> None:
        items = session.info.pop(_EMAIL_KEY, None)
        if not items:
            return
        from app.routers.auth import _send_email_direct
        from app.services.notification_dispatch import _pool
        for to, subject, body in items:
            _pool.submit(_send_email_direct, to, subject, body)

    @event.listens_for(Session, "after_rollback")
    def _discard(session: Session) -> None:
        session.info.pop(_EMAIL_KEY, None)


_register_email_hooks()
