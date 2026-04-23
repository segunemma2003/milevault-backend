import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.database import Base


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(300), nullable=False)
    message = Column(Text, nullable=False)
    type = Column(String(30), nullable=False)  # transaction | payment | dispute | kyc | system
    is_read = Column(Boolean, default=False)
    related_item_id = Column(String, nullable=True)
    related_item_type = Column(String(30), nullable=True)  # transaction | dispute | wallet | kyc
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="notifications")
