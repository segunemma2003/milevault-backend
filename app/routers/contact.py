"""
Contact form → support ticket.

Every message is stored as a ticket (nothing is lost if email fails), emailed to the
support inbox with Reply-To set to the customer, and acknowledged to the customer.
Admins manage tickets at /admin/support.
"""
import html
import logging
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_admin, require_admin
from app.models.support import SupportReply, SupportTicket
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["contact"])


class ContactRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    phone: Optional[str] = Field(None, max_length=50)
    subject: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=10, max_length=5000)


def _ref() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "BDR-" + "".join(secrets.choice(alphabet) for _ in range(6))


def _send(to_email: str, subject: str, body_html: str, reply_to: Optional[str] = None,
          as_support: bool = False) -> None:
    """Direct Resend call (no Celery dependency). Logs the outcome.
    as_support=True sends from "Bondrova Support <support@…>" so customer replies come back to the inbox."""
    from app.services.email_template import render_email, sender
    if not settings.RESEND_API_KEY:
        logger.warning("[SUPPORT EMAIL NOT SENT — no RESEND_API_KEY] to=%s subject=%r", to_email, subject)
        return
    try:
        import httpx
        from_addr = f"Bondrova Support <{settings.SUPPORT_FROM_EMAIL}>" if as_support else sender()
        payload = {"from": from_addr, "to": [to_email], "subject": subject, "html": render_email(body_html)}
        if reply_to:
            payload["reply_to"] = reply_to
        r = httpx.post("https://api.resend.com/emails", json=payload,
                       headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"}, timeout=10)
        if r.is_success:
            logger.warning("[SUPPORT EMAIL SENT] to=%s subject=%r", to_email, subject)
        else:
            logger.error("[SUPPORT EMAIL FAILED] to=%s %s %s", to_email, r.status_code, r.text[:300])
    except Exception as exc:
        logger.error("[SUPPORT EMAIL FAILED] to=%s %r", to_email, exc)


def _notify_support(ref: str, name: str, email: str, phone: Optional[str], subject: str, message: str) -> None:
    body = (
        f"<h2>New support request {html.escape(ref)}</h2>"
        f"<p><strong>From:</strong> {html.escape(name)} &lt;{html.escape(email)}&gt;</p>"
        f"<p><strong>Phone:</strong> {html.escape(phone or 'N/A')}</p>"
        f"<p><strong>Subject:</strong> {html.escape(subject)}</p><hr/>"
        f"<p>{html.escape(message).replace(chr(10), '<br/>')}</p>"
        f"<p style='color:#6b7280'>Reply from the Admin → Support inbox, or reply to this email directly.</p>"
    )
    _send(settings.SUPPORT_INBOX_EMAIL, f"[{ref}] {subject} — {name}", body, reply_to=email)


def _ack_customer(ref: str, name: str, email: str, subject: str) -> None:
    body = (
        f"<h2>We've received your message</h2>"
        f"<p>Hi {html.escape(name.split(' ')[0])},</p>"
        f"<p>Thanks for contacting Bondrova support. Your reference is <strong>{html.escape(ref)}</strong>.</p>"
        f"<p>We usually reply within one business day (Mon–Fri, 8am–5pm WAT).</p>"
        f"<p style='color:#6b7280'>Subject: {html.escape(subject)}</p>"
    )
    _send(email, f"[{ref}] We've received your message", body, as_support=True)


@router.post("/contact", status_code=200)
def submit_contact(payload: ContactRequest, background: BackgroundTasks, db: Session = Depends(get_db)):
    ticket = SupportTicket(reference=_ref(), name=payload.name.strip(), email=payload.email.lower(),
                           phone=(payload.phone or "").strip() or None, subject=payload.subject.strip(),
                           message=payload.message.strip())
    user = db.query(User).filter(User.email == ticket.email).first()
    if user:
        ticket.user_id = user.id
    db.add(ticket)
    db.commit()
    background.add_task(_notify_support, ticket.reference, ticket.name, ticket.email, ticket.phone,
                        ticket.subject, ticket.message)
    background.add_task(_ack_customer, ticket.reference, ticket.name, ticket.email, ticket.subject)
    return {"message": f"Your message has been received (ref {ticket.reference}). We'll get back to you within one business day.",
            "reference": ticket.reference}


# ─── Admin inbox ──────────────────────────────────────────────────────────────

def _ticket_out(t: SupportTicket, replies=None):
    out = {"id": t.id, "reference": t.reference, "name": t.name, "email": t.email, "phone": t.phone,
           "subject": t.subject, "message": t.message, "status": t.status, "user_id": t.user_id,
           "created_at": t.created_at, "updated_at": t.updated_at}
    if replies is not None:
        out["replies"] = [{"id": r.id, "author_role": r.author_role, "body": r.body, "created_at": r.created_at}
                          for r in replies]
        out["origin"] = "outbound" if t.message.startswith("[Sent by Bondrova Support]") else "inbound"
    return out


@router.get("/admin/support")
def list_tickets(status: str = "open", db: Session = Depends(get_db), _: User = Depends(require_admin("support"))):
    q = db.query(SupportTicket)
    if status != "all":
        q = q.filter(SupportTicket.status == status)
    return [_ticket_out(t) for t in q.order_by(SupportTicket.created_at.desc()).limit(500).all()]


@router.get("/admin/support/{ticket_id}")
def get_ticket(ticket_id: str, db: Session = Depends(get_db), _: User = Depends(require_admin("support"))):
    t = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    replies = db.query(SupportReply).filter(SupportReply.ticket_id == t.id).order_by(SupportReply.created_at).all()
    return _ticket_out(t, replies)


@router.post("/admin/support/{ticket_id}/reply")
def reply(ticket_id: str, background: BackgroundTasks, body: str = Body(..., embed=True, min_length=1, max_length=10000),
          close: bool = Body(False, embed=True), db: Session = Depends(get_db), admin: User = Depends(require_admin("support"))):
    t = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    db.add(SupportReply(ticket_id=t.id, author_id=admin.id, author_role="admin", body=body.strip()))
    t.status = "closed" if close else "answered"
    t.updated_at = datetime.utcnow()
    db.commit()
    html_body = (
        f"<p>Hi {html.escape(t.name.split(' ')[0])},</p>"
        f"<p>{html.escape(body.strip()).replace(chr(10), '<br/>')}</p>"
        f"<p style='color:#6b7280'>— Bondrova Support · Ref {html.escape(t.reference)}<br/>"
        f"Reply to this email if you need anything else.</p>"
    )
    background.add_task(_send, t.email, f"Re: [{t.reference}] {t.subject}", html_body, None, True)
    return {"ok": True, "status": t.status}


@router.put("/admin/support/{ticket_id}/status")
def set_status(ticket_id: str, status: str = Body(..., embed=True), db: Session = Depends(get_db),
               _: User = Depends(require_admin("support"))):
    if status not in ("open", "answered", "closed"):
        raise HTTPException(status_code=422, detail="Invalid status")
    t = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    t.status = status
    db.commit()
    return {"ok": True}


# ─── Inbound email (Cloudflare Email Worker → here) ───────────────────────────

import re as _re
from email import policy as _policy
from email.parser import BytesParser as _BytesParser
from email.utils import parseaddr as _parseaddr

from fastapi import Header as _Header, Request as _Request

_REF_RE = _re.compile(r"\[(BDR-[A-Z0-9]{6})\]")


def _strip_quoted(text: str) -> str:
    """Drop the quoted previous message most mail apps add below a reply."""
    out = []
    for line in text.splitlines():
        if _re.match(r"^\s*On .+ wrote:\s*$", line) or line.strip().startswith("-----Original Message-----"):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _plain_body(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        content = _re.sub(r"<br\s*/?>|</p>", "\n", content, flags=_re.I)
        content = html.unescape(_re.sub(r"<[^>]+>", "", content))
    return content


def _notify_admins(db: Session, title: str, message: str, ticket_id: str) -> None:
    from app.services.notification_service import create_notification
    for admin in db.query(User).filter(User.is_admin.is_(True)).all():
        create_notification(db, str(admin.id), title, message, "support", ticket_id, "support_ticket")
    db.commit()


@router.post("/support/inbound", include_in_schema=False)
async def inbound_email(request: _Request, db: Session = Depends(get_db),
                        x_inbound_secret: Optional[str] = _Header(default=None)):
    """
    Raw email (RFC 822) posted by the Cloudflare Email Worker for support@.
    A subject containing [BDR-XXXXXX] is added to that ticket's thread; anything else opens a new ticket.
    """
    import hmac as _hmac
    if not settings.SUPPORT_INBOUND_SECRET or not x_inbound_secret or \
            not _hmac.compare_digest(settings.SUPPORT_INBOUND_SECRET, x_inbound_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    raw = await request.body()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Email too large")

    msg = _BytesParser(policy=_policy.default).parsebytes(raw)
    from_name, from_email = _parseaddr(str(msg.get("From", "")))
    from_email = (from_email or "").lower()
    subject = str(msg.get("Subject", "") or "(no subject)").strip()[:200]

    # Our own system mail (ticket alerts from noreply@, mail sent as support@) must not loop back in.
    if from_email.endswith("@bondrova.com"):
        return {"ok": True, "outcome": "ignored_own_domain"}

    body = _strip_quoted(_plain_body(msg))[:20000] or "(empty message)"

    m = _REF_RE.search(subject)
    ticket = db.query(SupportTicket).filter(SupportTicket.reference == m.group(1)).first() if m else None
    if ticket and ticket.email.lower() == from_email:
        db.add(SupportReply(ticket_id=ticket.id, author_role="customer", body=body))
        ticket.status = "open"
        ticket.updated_at = datetime.utcnow()
        db.commit()
        _notify_admins(db, f"Customer replied — {ticket.reference}", f"{ticket.name}: {body[:140]}", ticket.id)
        return {"ok": True, "outcome": "reply_added", "reference": ticket.reference}

    ticket = SupportTicket(reference=_ref(), name=(from_name or from_email.split("@")[0])[:120], email=from_email,
                           subject=subject, message=body)
    user = db.query(User).filter(User.email == from_email).first()
    if user:
        ticket.user_id = user.id
    db.add(ticket)
    db.commit()
    _notify_admins(db, f"New support email — {ticket.reference}", f"{ticket.name}: {subject}", ticket.id)
    return {"ok": True, "outcome": "ticket_created", "reference": ticket.reference}


# ─── Compose: email anyone as Bondrova Support ────────────────────────────────

class ComposeRequest(BaseModel):
    to: EmailStr
    name: Optional[str] = Field(None, max_length=120)
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=10000)


@router.post("/admin/support/compose")
def compose(payload: ComposeRequest, background: BackgroundTasks, db: Session = Depends(get_db),
            admin: User = Depends(require_admin("support"))):
    """Start a new conversation from support@. It becomes a ticket so the customer's reply threads back."""
    to = payload.to.lower()
    user = db.query(User).filter(User.email == to).first()
    name = (payload.name or (f"{user.first_name} {user.last_name}" if user else to.split("@")[0])).strip()
    ticket = SupportTicket(reference=_ref(), name=name[:120], email=to, subject=payload.subject.strip(),
                           message="[Sent by Bondrova Support]", status="answered",
                           user_id=user.id if user else None)
    db.add(ticket)
    db.flush()
    db.add(SupportReply(ticket_id=ticket.id, author_id=admin.id, author_role="admin", body=payload.body.strip()))
    db.commit()
    html_body = (
        f"<p>Hi {html.escape(name.split(' ')[0])},</p>"
        f"<p>{html.escape(payload.body.strip()).replace(chr(10), '<br/>')}</p>"
        f"<p style='color:#6b7280'>— Bondrova Support · Ref {html.escape(ticket.reference)}</p>"
    )
    background.add_task(_send, to, f"[{ticket.reference}] {ticket.subject}", html_body, None, True)
    return {"ok": True, "id": ticket.id, "reference": ticket.reference}
