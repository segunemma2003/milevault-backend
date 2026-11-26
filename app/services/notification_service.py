from sqlalchemy.orm import Session
from app.models.notification import Notification


def create_notification(
    db: Session,
    user_id: str,
    title: str,
    message: str,
    type: str,
    related_item_id: str = None,
    related_item_type: str = None,
):
    notification = Notification(
        user_id=user_id,
        title=title,
        message=message,
        type=type,
        related_item_id=related_item_id,
        related_item_type=related_item_type,
    )
    db.add(notification)
    # Saved with the caller's work when the CALLER commits (and discarded if it rolls back) — committing
    # here used to save half-finished operations, e.g. a payment release before its fee and receipt.
    # Live push + email happen after commit in app/services/notification_dispatch.py
    db.flush()
    return notification
