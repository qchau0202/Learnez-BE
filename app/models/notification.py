"""Pydantic models aligned with public.notifications (Supabase)."""

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

NotificationTypeLiteral = Literal["course", "system", "reminder"]


class NotificationOut(BaseModel):
    id: int
    created_at: datetime
    recipient_id: str
    title: str
    body: str
    notification_type: NotificationTypeLiteral
    is_read: bool = False
    read_at: Optional[datetime] = None
    is_pinned: bool = False
    course_id: Optional[int] = None
    scenario: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: Optional[str] = None

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_dict(cls, v: Any) -> Any:
        return v if isinstance(v, dict) else {}

    class Config:
        from_attributes = True


class NotificationCreate(BaseModel):
    recipient_id: str
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=5000)
    notification_type: NotificationTypeLiteral
    course_id: Optional[int] = None
    is_pinned: bool = False
    scenario: Optional[str] = Field(
        None,
        description="Optional manual scenario key (e.g. dropout_risk_note, course_announcement).",
    )
    metadata: Optional[dict[str, Any]] = None


class NotificationUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    body: Optional[str] = Field(None, min_length=1, max_length=5000)
    notification_type: Optional[NotificationTypeLiteral] = None
    is_read: Optional[bool] = None
    read_at: Optional[datetime] = None
    is_pinned: Optional[bool] = None
    course_id: Optional[int] = None
    scenario: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class NotificationIdsIn(BaseModel):
    ids: List[int] = Field(min_length=1, max_length=200)


class NotificationRecipientUpdate(BaseModel):
    """Fields a recipient may change on their own notifications."""

    is_read: Optional[bool] = None
    read_at: Optional[datetime] = None
    is_pinned: Optional[bool] = None


class DemoLowAttendanceIn(BaseModel):
    student_id: str
    course_id: int
    note: Optional[str] = Field(None, max_length=5000)
