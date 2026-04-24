from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class WalletBalanceOut(BaseModel):
    id: str
    currency: str
    amount: float
    pending_amount: float

    model_config = {"from_attributes": True}


class WalletTransactionOut(BaseModel):
    id: str
    user_id: str
    type: str
    amount: float
    currency: str
    status: str
    description: Optional[str] = None
    transaction_id: Optional[str] = None
    method: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DepositRequest(BaseModel):
    amount: float
    currency: str = "USD"
    method: str = "card"


class WithdrawRequest(BaseModel):
    amount: float
    currency: str = "USD"
    method: str = "bank"
    bank_details: str = ""
    otp_code: Optional[str] = None  # required when 2FA is enabled on the account


class ConvertRequest(BaseModel):
    from_currency: str
    to_currency: str
    amount: float


class TransferRequest(BaseModel):
    recipient_email: str
    amount: float
    currency: str = "USD"
    note: str = ""
