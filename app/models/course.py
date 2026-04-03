"""Pydantic models aligned with public.courses and related tables."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CourseBase(BaseModel):
    title: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=2000)
    course_code: Optional[str] = None
    semester: Optional[str] = None
    lecturer_id: Optional[str] = None
    schedule: Optional[datetime] = None
    is_complete: bool = False


class CourseCreate(CourseBase):
    """Payload for creating a course (matches courses insert)."""

    title: str = Field(min_length=1, max_length=500)


class CourseUpdate(BaseModel):
    """Partial update — omit fields you do not change."""

    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = None
    course_code: Optional[str] = None
    semester: Optional[str] = None
    lecturer_id: Optional[str] = None
    schedule: Optional[datetime] = None
    is_complete: Optional[bool] = None


class CourseOut(BaseModel):
    """Row from public.courses."""

    id: int
    created_at: datetime
    title: str
    description: str
    lecturer_id: Optional[str] = None
    schedule: Optional[datetime] = None
    is_complete: Optional[bool] = False
    course_code: Optional[str] = None
    created_by: Optional[str] = None
    semester: Optional[str] = None

    class Config:
        from_attributes = True


class ModuleBase(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    course_id: Optional[int] = None


class ModuleOut(ModuleBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class AssignmentBase(BaseModel):
    module_id: Optional[int] = None
    title: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    total_score: Optional[float] = None


class AssignmentOut(AssignmentBase):
    id: int
    created_at: datetime
    uploaded_by: Optional[int] = None
    achieved_score: Optional[float] = None
    is_complete: Optional[bool] = False

    class Config:
        from_attributes = True


class CourseEnrollmentOut(BaseModel):
    course_id: int
    student_id: str
    created_at: datetime

    class Config:
        from_attributes = True
