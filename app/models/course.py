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

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "Software Engineering Fundamentals",
                "description": "Core software engineering concepts and practices.",
                "course_code": "SE-101",
                "semester": "1",
                "academic_year": "2025-2026",
                "lecturer_id": "b9dd5f4f-40a8-4d5a-8f5d-c63c7a9f440d",
                "is_complete": False,
            }
        }
    }


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

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "Software Engineering Fundamentals (Updated)",
                "description": "Updated outline and timeline.",
                "is_complete": False,
            }
        }
    }


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

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "Module 1 - Requirements Engineering",
                "description": "Elicitation, analysis, and requirement specs.",
            }
        }
    }


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
