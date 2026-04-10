"""Pydantic models aligned with public.courses and related tables."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CourseBase(BaseModel):
    title: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=2000)
    course_code: Optional[str] = None
    semester: Optional[str] = None
    academic_year: Optional[str] = None
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
    academic_year: Optional[str] = None
    lecturer_id: Optional[str] = None
    schedule: Optional[datetime] = None
    is_complete: Optional[bool] = None


class CourseOut(BaseModel):
    """Row from public.courses."""

    id: int
    created_at: datetime
    title: str
    description: str
    academic_year: Optional[str] = None
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


class ModuleCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=2000)


class ModuleOut(ModuleBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class CourseEnrollmentOut(BaseModel):
    course_id: int
    student_id: str
    created_at: datetime

    class Config:
        from_attributes = True


class MaterialOut(BaseModel):
    id: int
    module_id: Optional[int] = None
    created_at: datetime
    material_type: Optional[str] = None
    file_url: Optional[str] = None
    uploaded_by: Optional[str] = None

    class Config:
        from_attributes = True
