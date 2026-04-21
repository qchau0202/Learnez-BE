"""Pydantic models for public.course_attendance."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class AttendanceStatus(str, Enum):
    """Allowed values for `course_attendance.status`."""

    PRESENT = "Present"
    LATE = "Late"
    ABSENT = "Absent"


class AttendanceRecordIn(BaseModel):
    """One student's attendance entry inside a bulk upsert payload."""

    student_id: str = Field(..., description="FK → users.user_id (student)")
    status: AttendanceStatus
    notes: Optional[str] = Field(None, max_length=2000)


class AttendanceBulkUpsertIn(BaseModel):
    """Bulk upsert payload used by lecturers / admins to save a roll-call."""

    session_date: str = Field(
        ...,
        description="Session date as ISO 8601 string (e.g. '2026-09-07' or '2026-09-07T00:00:00Z').",
    )
    records: list[AttendanceRecordIn]

    model_config = {
        "json_schema_extra": {
            "example": {
                "session_date": "2026-09-07",
                "records": [
                    {"student_id": "b9dd5f4f-40a8-4d5a-8f5d-c63c7a9f440d", "status": "Present"},
                    {"student_id": "c1e3c9cf-2a02-4f6a-9b3d-5c8d6e2e8cef", "status": "Late", "notes": "Arrived 10 min late"},
                ],
            }
        }
    }


class AttendanceRecordOut(BaseModel):
    """Row from public.course_attendance."""

    id: int
    created_at: datetime
    recorded_by: Optional[str] = None
    student_id: Optional[str] = None
    course_id: Optional[int] = None
    status: Optional[str] = None
    session_date: Optional[datetime] = None
    notes: Optional[str] = None

    class Config:
        from_attributes = True


class AttendanceSessionStudentRow(BaseModel):
    """One row of the lecturer's roll-call view for a given session date."""

    student_id: str = Field(..., description="FK → users.user_id")
    full_name: Optional[str] = None
    email: Optional[str] = None
    student_class: Optional[str] = Field(
        None,
        description="Student cohort from `student_profiles.class` (not course classroom).",
    )
    attendance_id: Optional[int] = None
    status: Optional[str] = Field(
        None,
        description="'Present' | 'Late' | 'Absent'. Null if never recorded for this session.",
    )
    notes: Optional[str] = None
    recorded_by: Optional[str] = None
    recorded_at: Optional[datetime] = None


class AttendanceSessionOut(BaseModel):
    """Per-session roll-call response."""

    course_id: int
    session_date: str = Field(..., description="Echoed back as ISO date (YYYY-MM-DD).")
    records: list[AttendanceSessionStudentRow]
