from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class NotificationOut(BaseModel):
    id: str
    user_id: str
    title: str
    message: str
    type: str
    is_read: bool
    related_item_id: Optional[str] = None
    related_item_type: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DashboardStats(BaseModel):
    total_transactions: int
    active_transactions: int
    completed_transactions: int
    pending_approval: int
    total_disputes: int
    wallet_balance: list
    pending_withdrawals: int
    available_to_withdraw: float
