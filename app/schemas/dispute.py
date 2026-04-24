from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class DisputeCreate(BaseModel):
    transaction_id: str
    milestone_id: Optional[str] = None
    title: str
    description: str = Field(..., min_length=20)
    reason: str
    suggested_resolution: Optional[str] = None
    evidence_urls: List[str] = Field(
        ...,
        min_length=1,
        description="At least one evidence URL (https link or s3: key from upload).",
    )


class DisputeUpdate(BaseModel):
    status: Optional[str] = None
    resolution: Optional[str] = None


class DisputeDocumentCreate(BaseModel):
    s3_key: str
    filename: str = "evidence"


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
