import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


class KycDocument(Base):
    __tablename__ = "kyc_documents"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(30), nullable=False)
    # id_card | passport | drivers_license | address_proof | selfie
    file_url = Column(String, nullable=False)
    file_name = Column(String(255), nullable=True)
    status = Column(String(20), default="pending")  # pending | verified | rejected
    rejection_reason = Column(Text, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="kyc_documents")
