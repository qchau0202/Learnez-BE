"""Profile table models used for email->user role assignment flows."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class LecturerProfile(BaseModel):
    user_id: str
    email: Optional[str] = None
    created_at: Optional[datetime] = None


class StudentProfile(BaseModel):
    user_id: str
    email: Optional[str] = None
    created_at: Optional[datetime] = None
