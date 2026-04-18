"""Pydantic models aligned with public.courses and related tables."""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class CourseBase(BaseModel):
    title: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=2000)
    course_code: Optional[str] = None
    semester: Optional[str] = None
    academic_year: Optional[str] = None
    class_room: Optional[str] = Field(
        None,
        max_length=200,
        description="Section / room label; distinguishes offerings that share the same course code.",
    )
    course_occurences: Optional[int] = Field(None, ge=1, le=60)
    course_session: Optional[str] = Field(None, max_length=120)
    course_start_date: Optional[date] = None
    course_end_date: Optional[date] = None
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
                "class_room": "A1-201",
                "course_occurences": 15,
                "course_session": "7:00 - 9:30",
                "course_start_date": "2026-09-01",
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
    class_room: Optional[str] = Field(None, max_length=200)
    course_occurences: Optional[int] = Field(None, ge=1, le=60)
    course_session: Optional[str] = Field(None, max_length=120)
    course_start_date: Optional[date] = None
    course_end_date: Optional[date] = None
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
    class_room: Optional[str] = None
    course_occurences: Optional[int] = None
    course_session: Optional[str] = None
    course_start_date: Optional[date] = None
    course_end_date: Optional[date] = None

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


class CourseActorOut(BaseModel):
    id: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    lecturer_id: Optional[str] = None
    faculty_id: Optional[int] = None
    faculty_name: Optional[str] = None
    department_id: Optional[int] = None
    department_name: Optional[str] = None
    student_class: Optional[str] = Field(
        None,
        description="Student cohort from student_profiles.class (distinct from course classroom).",
    )


class CourseAdminItemOut(CourseOut):
    student_count: int = 0
    faculty_id: Optional[int] = None
    faculty_name: Optional[str] = None
    department_id: Optional[int] = None
    department_name: Optional[str] = None


class CourseAdminManagementOut(BaseModel):
    courses: list[CourseAdminItemOut]
    lecturers: list[CourseActorOut]
    students: list[CourseActorOut]


class MaterialOut(BaseModel):
    id: int
    module_id: Optional[int] = None
    created_at: datetime
    material_type: Optional[str] = None
    file_url: Optional[str] = None
    uploaded_by: Optional[str] = None

    class Config:
        from_attributes = True
