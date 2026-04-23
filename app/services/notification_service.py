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
    db.commit()
    return notification
