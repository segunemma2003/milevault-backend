from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Any
from datetime import datetime
from app.schemas.user import UserPublic


class FundMilestoneBody(BaseModel):
    """Omit amount to fund the remaining balance for this milestone."""
    amount: Optional[float] = None


class DeliverySubmit(BaseModel):
    delivery_title: str = Field(..., min_length=3, max_length=300)
    delivery_note: str = Field(..., min_length=10, description="What was delivered and how to verify it")
    delivery_attachments: List[str] = Field(default_factory=list, description="Uploaded file keys or URLs")
    delivery_external_links: List[str] = Field(default_factory=list)
    delivery_version_notes: Optional[str] = None

    @model_validator(mode="after")
    def require_files_or_links_plus_note(self):
        att = [a for a in (self.delivery_attachments or []) if isinstance(a, str) and a.strip()]
        links = [u for u in (self.delivery_external_links or []) if isinstance(u, str) and u.strip()]
        if att:
            return self
        if links:
            # External proof: still require substantive note (already min 10)
            for u in links:
                u2 = u.strip().lower()
                if not (u2.startswith("http://") or u2.startswith("https://")):
                    raise ValueError("Each external link must start with http:// or https://")
            return self
        raise ValueError("Provide at least one delivery file (upload) or at least one valid external https link.")


class BuyerChecklist(BaseModel):
    files_received: bool = False
    matches_description: bool = False
    meets_requirements: bool = False


class ApproveMilestoneBody(BaseModel):
    feedback: Optional[str] = None
    checklist: Optional[BuyerChecklist] = None


class InvalidDeliveryReport(BaseModel):
    note: str = Field(..., min_length=10)


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
