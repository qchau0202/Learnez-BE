"""Course Management - Admin CRUD, Lecturer edit, Student view."""

from fastapi import APIRouter, Depends

from app.api.deps import DbDep

router = APIRouter(prefix="/courses", tags=["Course & Content - Courses"])


@router.post("/")
async def create_course(db: DbDep):
    """Admin: Create course."""
    ...


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
