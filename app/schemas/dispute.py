from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class DisputeCreate(BaseModel):
    transaction_id: str
    title: str
    description: str
    reason: str
    suggested_resolution: Optional[str] = None


class DisputeUpdate(BaseModel):
    status: Optional[str] = None
    resolution: Optional[str] = None


class DisputeDocumentOut(BaseModel):
    id: str
    file_url: str
    file_name: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DisputeOut(BaseModel):
    id: str
    transaction_id: str
    raised_by: str
    title: str
    description: str
    reason: str
    suggested_resolution: Optional[str] = None
    status: str
    resolution: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    documents: Optional[List[DisputeDocumentOut]] = []

    model_config = {"from_attributes": True}
