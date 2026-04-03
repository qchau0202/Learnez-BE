"""Course Management - Admin CRUD, Lecturer edit, Student view."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import DbDep
from app.core.database import get_supabase
from app.core.dependencies import require_roles

router = APIRouter(prefix="/courses", tags=["Course & Content - Courses"])


class CourseStudentEnrollmentCreateRequest(BaseModel):
    student_id: str
    enrollment_date: str
    enrollment_status: bool

class CourseModuleMaterialCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: str = Field(min_length=1, max_length=255)
    material_type: str = Field(min_length=1, max_length=255)
    file_url: str = Field(min_length=1, max_length=255)
    upload_by: str

class CourseModuleAssignmentEssayQuestionCreateRequest(BaseModel):
    question: str = Field(min_length=1, max_length=255)
    answer: str = Field(min_length=1, max_length=255)
    score: int

class CourseModuleAssignmentMCQQuestionCreateRequest(BaseModel):
    question: str = Field(min_length=1, max_length=255)
    answer: str = Field(min_length=1, max_length=255)
    score: int

class CourseModuleAssignmentCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    assignment_type: str = Field(min_length=1, max_length=255)
    upload_by: str
    due_date: str
    max_attempts: int
    attempts: int
    score: int
    essay_list: list[CourseModuleAssignmentEssayQuestionCreateRequest]
    mcq_list: list[CourseModuleAssignmentMCQQuestionCreateRequest]
    
class CourseModuleCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    material_list: list[CourseModuleMaterialCreateRequest]
    assignment_list: list[CourseModuleAssignmentCreateRequest]

class CourseCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    lecturer_id: str
    module_list: list[CourseModuleCreateRequest]
    student_list: list[CourseStudentEnrollmentCreateRequest]


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_course(
    payload: CourseCreateRequest,
    db: DbDep,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    """
    Admin: Create course shell record.
    """
    supabase = get_supabase(service_role=True)
    if not supabase:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")

    created = (
        supabase.table("courses")
        .insert(
            {
                "title": payload.title,
                "description": payload.description,
                "created_by": user["user_id"],
                "lecturer_id": payload.lecturer_id,
                "module_list": payload.module_list,
                "student_list": payload.student_list,
            }
        )
        .execute()
    )
    if not created.data:
        raise HTTPException(status_code=500, detail="Failed to create course")

    return {"message": "Course created", "course": created.data[0]}


@router.get("/")
async def list_courses(db: DbDep):
    """List courses (filtered by role)."""
    ...


@router.get("/{course_id}")
async def get_course(db: DbDep, course_id: str):
    """Get course by ID."""
    ...


@router.put("/{course_id}")
async def update_course(db: DbDep, course_id: str):
    """Admin/Lecturer: Edit course content."""
    ...


@router.delete("/{course_id}")
async def delete_course(db: DbDep, course_id: str):
    """Admin: Delete course."""
    ...
