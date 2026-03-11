"""Enrollment - Admin manages class enrollment."""

from fastapi import APIRouter, Depends

from app.api.deps import DbDep

router = APIRouter(prefix="/enrollment", tags=["Course & Content - Enrollment"])


@router.post("/{course_id}/students/{student_id}")
async def add_student(db: DbDep, course_id: str, student_id: str):
    """Admin: Add student to class."""
    ...


@router.delete("/{course_id}/students/{student_id}")
async def remove_student(db: DbDep, course_id: str, student_id: str):
    """Admin: Remove student from class."""
    ...


@router.get("/{course_id}/students")
async def list_enrolled_students(db: DbDep, course_id: str):
    """Simulated API: Fetch registered student list for a course."""
    ...
