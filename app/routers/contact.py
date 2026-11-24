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
from app.dependencies import get_current_admin
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


def _send(to_email: str, subject: str, body_html: str, reply_to: Optional[str] = None) -> None:
    """Direct Resend call (no Celery dependency). Logs the outcome."""
    from app.services.email_template import render_email, sender
    if not settings.RESEND_API_KEY:
        logger.warning("[SUPPORT EMAIL NOT SENT — no RESEND_API_KEY] to=%s subject=%r", to_email, subject)
        return
    try:
        import httpx
        payload = {"from": sender(), "to": [to_email], "subject": subject, "html": render_email(body_html)}
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
    _send(email, f"[{ref}] We've received your message", body)


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
    return out


@router.get("/admin/support")
def list_tickets(status: str = "open", db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    q = db.query(SupportTicket)
    if status != "all":
        q = q.filter(SupportTicket.status == status)
    return [_ticket_out(t) for t in q.order_by(SupportTicket.created_at.desc()).limit(500).all()]


@router.get("/admin/support/{ticket_id}")
def get_ticket(ticket_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_admin)):
    t = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    replies = db.query(SupportReply).filter(SupportReply.ticket_id == t.id).order_by(SupportReply.created_at).all()
    return _ticket_out(t, replies)


@router.post("/admin/support/{ticket_id}/reply")
def reply(ticket_id: str, background: BackgroundTasks, body: str = Body(..., embed=True, min_length=1, max_length=10000),
          close: bool = Body(False, embed=True), db: Session = Depends(get_db), admin: User = Depends(get_current_admin)):
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
    background.add_task(_send, t.email, f"Re: [{t.reference}] {t.subject}", html_body, settings.SUPPORT_INBOX_EMAIL)
    return {"ok": True, "status": t.status}


@router.put("/admin/support/{ticket_id}/status")
def set_status(ticket_id: str, status: str = Body(..., embed=True), db: Session = Depends(get_db),
               _: User = Depends(get_current_admin)):
    if status not in ("open", "answered", "closed"):
        raise HTTPException(status_code=422, detail="Invalid status")
    t = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    t.status = status
    db.commit()
    return {"ok": True}
