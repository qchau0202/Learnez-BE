"""Enrollment — register students on courses (Supabase public.course_enrollments)."""

from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, require_roles
from app.models.course import CourseEnrollmentOut
from app.services.notifications.scenario_notifications import (
    notify_enrollment_added,
    notify_enrollment_removed,
)

router = APIRouter(prefix="/enrollment", tags=["Course & Content - Enrollment"])


def _sb():
    supabase = get_supabase(service_role=True)
    if not supabase:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")
    return supabase


def _can_edit_course(user: dict[str, Any], course_row: dict) -> bool:
    role = ROLE_MAP.get(user.get("role_id"))
    uid = user.get("user_id")
    if role == "Admin":
        return True
    if role == "Lecturer" and course_row.get("lecturer_id") == uid:
        return True
    return False


@router.post(
    "/{course_id}/students/{student_id}",
    response_model=CourseEnrollmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_student(
    course_id: int,
    student_id: str,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    crs = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not crs.data:
        raise HTTPException(status_code=404, detail="Course not found")
    if not _can_edit_course(user, crs.data[0]):
        raise HTTPException(status_code=403, detail="Forbidden")

    existing = (
        sb.table("course_enrollments")
        .select("course_id")
        .eq("course_id", course_id)
        .eq("student_id", student_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="Student already enrolled")

    ins = (
        sb.table("course_enrollments")
        .insert({"course_id": course_id, "student_id": student_id})
        .execute()
    )
    if not ins.data:
        raise HTTPException(status_code=500, detail="Failed to enroll student")
    notify_enrollment_added(sb, student_id=student_id, course_id=course_id)
    return ins.data[0]


@router.delete("/{course_id}/students/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_student(
    course_id: int,
    student_id: str,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    crs = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not crs.data:
        raise HTTPException(status_code=404, detail="Course not found")
    if not _can_edit_course(user, crs.data[0]):
        raise HTTPException(status_code=403, detail="Forbidden")
    sb.table("course_enrollments").delete().eq("course_id", course_id).eq("student_id", student_id).execute()
    notify_enrollment_removed(sb, student_id=student_id, course_id=course_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{course_id}/students", response_model=List[CourseEnrollmentOut])
async def list_enrolled_students(
    course_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    crs = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not crs.data:
        raise HTTPException(status_code=404, detail="Course not found")
    if not _can_edit_course(user, crs.data[0]):
        raise HTTPException(status_code=403, detail="Forbidden")
    enr = sb.table("course_enrollments").select("*").eq("course_id", course_id).execute()
    return enr.data or []
