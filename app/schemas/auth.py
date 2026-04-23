from pydantic import BaseModel, EmailStr
from typing import Optional


class RegisterRequest(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    password: str
    role: str = "buyer"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
