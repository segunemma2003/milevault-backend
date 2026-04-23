import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, ForeignKey, JSON, Boolean
from sqlalchemy.orm import relationship
from app.database import Base


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id = Column(String, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    message = Column(Text, nullable=False)
    attachments = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)

    transaction = relationship("Transaction", back_populates="chat_messages")
    sender = relationship("User", foreign_keys=[sender_id])


class DirectMessage(Base):
    __tablename__ = "direct_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    sender_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    recipient_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    message = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    sender = relationship("User", foreign_keys=[sender_id], back_populates="sent_messages")
    recipient = relationship("User", foreign_keys=[recipient_id], back_populates="received_messages")
