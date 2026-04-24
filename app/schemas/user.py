from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime


class UserOut(BaseModel):
    id: str
    first_name: str
    last_name: str
    name: str = ""
    email: str
    role: str
    avatar_url: Optional[str] = None
    is_kyc_verified: bool
    is_active: bool
    is_email_verified: bool
    is_admin: bool = False
    is_agent: bool = False
    phone: Optional[str] = None
    location: Optional[str] = None
    website: Optional[str] = None
    bio: Optional[str] = None
    country_code: Optional[str] = None
    completion_percentage: int = 60
    created_at: datetime

    model_config = {"from_attributes": True}

    def model_post_init(self, __context):
        self.name = f"{self.first_name} {self.last_name}"


class UserPublic(BaseModel):
    id: str
    name: str = ""
    first_name: str
    last_name: str
    email: str
    role: str
    avatar_url: Optional[str] = None
    is_kyc_verified: bool
    created_at: datetime

    model_config = {"from_attributes": True}

    def model_post_init(self, __context):
        self.name = f"{self.first_name} {self.last_name}"


class UpdateProfileRequest(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    website: Optional[str] = None
    bio: Optional[str] = None


class UserSettingsOut(BaseModel):
    dark_mode: str
    email_notifications: bool
    push_notifications: bool
    sms_notifications: bool
    transaction_notifications: bool
    marketing_notifications: bool
    security_notifications: bool
    default_currency: str
    two_factor_enabled: bool

    model_config = {"from_attributes": True}


class UpdateSettingsRequest(BaseModel):
    dark_mode: Optional[str] = None
    email_notifications: Optional[bool] = None
    push_notifications: Optional[bool] = None
    sms_notifications: Optional[bool] = None
    transaction_notifications: Optional[bool] = None
    marketing_notifications: Optional[bool] = None
    security_notifications: Optional[bool] = None
    default_currency: Optional[str] = None
    two_factor_enabled: Optional[bool] = None
