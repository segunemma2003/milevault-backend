"""
Every Notification row — however it was created (create_notification, admin/agent _notify
helpers, Celery tasks) — is delivered AFTER its transaction commits:

  1. pushed live to the user's open tabs (WebSocket via Redis), and
  2. emailed (branded template + button to the relevant page) for types that matter.

Rolled-back transactions deliver nothing. Delivery runs on a small thread pool so requests
aren't slowed down. Users who turned off email notifications still receive security,
payment and account emails (they're transactional).
"""
from __future__ import annotations

import html
import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event
from sqlalchemy.orm import Session

logger = logging.getLogger("bondrova.notify")
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")

# Types never emailed: admin support alerts already reach the support inbox/Gmail.
NO_EMAIL_TYPES = {"support", "chat", "message"}
# Always emailed even if the user turned email notifications off.
ALWAYS_EMAIL_TYPES = {"security", "payment", "account"}

_KEY = "bondrova_pending_notifications"


def _link_for(n: dict) -> str:
    from app.config import settings
    base = settings.FRONTEND_URL.rstrip("/")
    rid, rtype, ntype = n.get("related_item_id"), (n.get("related_item_type") or ""), (n.get("type") or "")
    if rid and rtype == "transaction":
        return f"{base}/dashboard/deals/{rid}"
    if rid and rtype == "dispute":
        return f"{base}/dashboard/disputes"
    if ntype == "payment" or rtype == "wallet":
        return f"{base}/dashboard/wallet"
    if ntype == "kyc":
        return f"{base}/dashboard/kyc"
    if ntype == "agent":
        return f"{base}/dashboard/agent"
    if ntype == "security":
        return f"{base}/dashboard/settings"
    return f"{base}/dashboard"


def _deliver(n: dict) -> None:
    # 1) live push
    try:
        from app.websocket.manager import publish_user_event
        publish_user_event(n["user_id"], {"type": "notification", "notification": n})
    except Exception as exc:  # pragma: no cover
        logger.debug("notification push failed: %s", exc)

    # 2) email
    ntype = (n.get("type") or "").lower()
    if ntype in NO_EMAIL_TYPES:
        return
    try:
        from app.config import settings
        if not settings.RESEND_API_KEY:
            return
        from app.database import SessionLocal
        from app.models.user import User, UserSettings
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.id == n["user_id"]).first()
            if not user or not user.email or not user.is_active:
                return
            prefs = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
            if prefs is not None and prefs.email_notifications is False and ntype not in ALWAYS_EMAIL_TYPES:
                return
            to_email, first_name = user.email, user.first_name
        finally:
            db.close()

        from app.routers.auth import _send_email_direct
        from app.services.email_template import button
        body = (
            f"<h2>{html.escape(n['title'])}</h2>"
            f"<p>Hi {html.escape(first_name or 'there')},</p>"
            f"<p>{html.escape(n['message']).replace(chr(10), '<br/>')}</p>"
            f"<p>{button(_link_for(n), 'Open Bondrova')}</p>"
            f"<p style='color:#6b7280;font-size:12px'>You're receiving this because of activity on your Bondrova account. "
            f"You can turn off non-essential emails in Settings → Notifications.</p>"
        )
        _send_email_direct(to_email, n["title"], body)
    except Exception as exc:
        logger.error("notification email failed for %s: %r", n.get("user_id"), exc)


@event.listens_for(Session, "after_flush")
def _collect(session: Session, flush_context) -> None:
    from app.models.notification import Notification
    pending = session.info.setdefault(_KEY, [])
    for obj in session.new:
        if isinstance(obj, Notification):
            pending.append({
                "id": str(obj.id), "user_id": str(obj.user_id), "title": obj.title, "message": obj.message,
                "type": obj.type, "related_item_id": obj.related_item_id, "related_item_type": obj.related_item_type,
            })


@event.listens_for(Session, "after_commit")
def _dispatch(session: Session) -> None:
    pending = session.info.pop(_KEY, None)
    for n in pending or []:
        _pool.submit(_deliver, n)


@event.listens_for(Session, "after_rollback")
def _discard(session: Session) -> None:
    session.info.pop(_KEY, None)
