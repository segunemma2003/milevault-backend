from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class KycDocumentOut(BaseModel):
    id: str
    user_id: str
    type: str
    file_url: str
    file_name: Optional[str] = None
    status: str
    rejection_reason: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class KycStatusOut(BaseModel):
    is_kyc_verified: bool
    documents: list[KycDocumentOut]
    overall_status: str  # pending | verified | rejected | incomplete
