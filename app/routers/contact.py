from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from typing import Optional
import logging

logger = logging.getLogger(__name__)

router = APIRouter(tags=["contact"])


class ContactRequest(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = None
    subject: str
    message: str


@router.post("/contact", status_code=200)
def submit_contact(payload: ContactRequest):
    if len(payload.name) < 2:
        raise HTTPException(status_code=422, detail="Name must be at least 2 characters.")
    if len(payload.message) < 10:
        raise HTTPException(status_code=422, detail="Message must be at least 10 characters.")

    body_html = (
        f"<h2>New Contact Form Submission</h2>"
        f"<p><strong>Name:</strong> {payload.name}</p>"
        f"<p><strong>Email:</strong> {payload.email}</p>"
        f"<p><strong>Phone:</strong> {payload.phone or 'N/A'}</p>"
        f"<p><strong>Subject:</strong> {payload.subject}</p>"
        f"<hr/><p>{payload.message.replace(chr(10), '<br/>')}</p>"
    )

    try:
        from app.services.tasks import send_notification_email
        from app.config import settings
        send_notification_email.delay(
            to_email=settings.ADMIN_EMAIL,
            subject=f"[Contact] {payload.subject} — {payload.name}",
            body_html=body_html,
        )
    except Exception as exc:
        logger.error(f"Contact form Celery dispatch failed: {exc}")

    return {"message": "Your message has been received. We'll get back to you within 24 hours."}
