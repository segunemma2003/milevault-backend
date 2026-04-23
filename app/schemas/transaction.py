from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime
from app.schemas.user import UserPublic


class MilestoneCreate(BaseModel):
    title: str
    description: Optional[str] = None
    amount: float
    currency: Optional[str] = None
    due_date: Optional[datetime] = None
    expectations: Optional[str] = None
    percentage_of_total: Optional[float] = None
    attachments: Optional[List[Any]] = []
    supporting_documents: Optional[List[Any]] = []


class MilestoneUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    amount: Optional[float] = None
    status: Optional[str] = None
    due_date: Optional[datetime] = None
    expectations: Optional[str] = None
    feedback: Optional[str] = None
    percentage_of_total: Optional[float] = None
    attachments: Optional[List[Any]] = None
    supporting_documents: Optional[List[Any]] = None


class MilestoneOut(BaseModel):
    id: str
    transaction_id: str
    title: str
    description: Optional[str] = None
    amount: float
    currency: Optional[str] = None
    status: str
    due_date: Optional[datetime] = None
    completed_date: Optional[datetime] = None
    expectations: Optional[str] = None
    feedback: Optional[str] = None
    percentage_of_total: Optional[float] = None
    attachments: Optional[List[Any]] = []
    supporting_documents: Optional[List[Any]] = []
    created_at: datetime

    model_config = {"from_attributes": True}


class TransactionCreate(BaseModel):
    title: str
    description: Optional[str] = None
    amount: float
    currency: str = "USD"
    type: str = "one_time"
    expected_completion_date: Optional[datetime] = None
    service_fee_payment: str = "buyer"
    buyer_fee_ratio: float = 100.0
    seller_fee_ratio: float = 0.0
    notes: Optional[str] = None
    counterparty_email: Optional[str] = None
    supporting_url: Optional[str] = None
    milestones: Optional[List[MilestoneCreate]] = []


class TransactionUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    expected_completion_date: Optional[datetime] = None
    notes: Optional[str] = None
    contract_signed: Optional[bool] = None
    supporting_url: Optional[str] = None


class AdditionalDetails(BaseModel):
    project_url: Optional[str] = None
    expected_delivery_date: Optional[str] = None
    payment_method: Optional[str] = None
    milestones_count: Optional[int] = None
    completed_milestones: Optional[int] = None
    terms_accepted: Optional[bool] = None
    supporting_documents: Optional[List[str]] = None
    notes: Optional[str] = None
    service_fee_payment: Optional[str] = None
    service_fee_ratio: Optional[dict] = None


class TransactionOut(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    amount: float
    currency: str
    type: str
    status: str
    buyer_id: str
    seller_id: Optional[str] = None
    buyer: Optional[UserPublic] = None
    seller: Optional[UserPublic] = None
    supporting_url: Optional[str] = None
    contract_signed: bool
    created_at: datetime
    updated_at: datetime
    additional_details: Optional[AdditionalDetails] = None
    milestones: Optional[List[MilestoneOut]] = []

    model_config = {"from_attributes": True}

    def model_post_init(self, __context):
        self.additional_details = AdditionalDetails(
            milestones_count=getattr(self, '_milestones_count', None),
            completed_milestones=getattr(self, '_completed_milestones', None),
        )
