"""User and account models for IAM (Module 1)."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class UserRole(str, Enum):
    ADMIN = "admin"
    LECTURER = "lecturer"
    STUDENT = "student"


# MongoDB document structure (stored in users collection)
class UserDocument(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    email: str
    full_name: str
    role: UserRole
    hashed_password: str
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[str] = None  # Admin who created the account

    class Config:
        populate_by_name = True


# API schemas
class UserCreate(BaseModel):
    email: str
    full_name: str
    role: UserRole
    password: str


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    role: UserRole
    is_active: bool


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
