from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime
from app.schemas.user import UserPublic


class ChatMessageCreate(BaseModel):
    message: str
    attachments: Optional[List[Any]] = []


class ChatMessageOut(BaseModel):
    id: str
    transaction_id: str
    sender_id: str
    sender: Optional[UserPublic] = None
    message: str
    attachments: Optional[List[Any]] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class DirectMessageCreate(BaseModel):
    recipient_id: str
    message: str


class DirectMessageOut(BaseModel):
    id: str
    sender_id: str
    recipient_id: str
    sender: Optional[UserPublic] = None
    recipient: Optional[UserPublic] = None
    message: str
    is_read: bool
    created_at: datetime

    model_config = {"from_attributes": True}
