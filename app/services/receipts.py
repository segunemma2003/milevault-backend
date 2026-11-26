"""
A copy of every money receipt (deposit credited, withdrawal sent) to Bondrova's own inbox, so the
company has a record of each one. The address is set by admin/finance in Fees & Settings
(blank = the support email). Sent after the database commit, off the request thread.
"""
from __future__ import annotations

import html
import logging
import threading

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def copy_address(db: Session) -> str | None:
    from app.config import settings
    from app.services.fees import platform_settings
    s = platform_settings(db)
    return (getattr(s, "receipts_copy_email", None) or s.support_email or settings.SUPPORT_INBOX_EMAIL or "").strip() or None


def send_copy(db: Session, *, kind: str, user_name: str, user_email: str, lines: list[tuple[str, str]]) -> None:
    to = copy_address(db)
    if not to:
        return
    rows = "".join(f"<tr><td style='padding:4px 12px 4px 0;color:#666'>{html.escape(k)}</td>"
                   f"<td style='padding:4px 0'><strong>{html.escape(v)}</strong></td></tr>" for k, v in lines)
    body = (f"<h2>{html.escape(kind)} — receipt copy</h2>"
            f"<p>{html.escape(user_name)} &lt;{html.escape(user_email)}&gt;</p><table>{rows}</table>")
    subject = f"[Receipt] {kind}: {lines[0][1] if lines else ''} — {user_name}"

    def _send():
        try:
            from app.routers.auth import _send_email_direct
            _send_email_direct(to, subject, body)
        except Exception as e:  # noqa: BLE001 — a copy must never break a payment
            log.warning("Receipt copy to %s failed: %s", to, e)

    threading.Thread(target=_send, daemon=True).start()
