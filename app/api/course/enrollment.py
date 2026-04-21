"""Enrollment — register students on courses (Supabase public.course_enrollments)."""

from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, require_roles
from app.models.course import CourseEnrollmentOut, EnrollmentRosterRow
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
    summary="Enroll student to course",
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


@router.delete("/{course_id}/students/{student_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Unenroll student from course")
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


def _roster_rows_for_student_ids(sb: Any, student_ids: list[str]) -> list[EnrollmentRosterRow]:
    if not student_ids:
        return []
    users_res = sb.table("users").select("user_id, full_name, email, role_id").in_("user_id", student_ids).execute()
    rows_out: list[EnrollmentRosterRow] = []
    sp_res = sb.table("student_profiles").select("user_id, class").in_("user_id", student_ids).execute()
    class_by_uid = {r["user_id"]: r.get("class") for r in (sp_res.data or []) if r.get("user_id")}
    for u in users_res.data or []:
        uid = u.get("user_id")
        if not uid:
            continue
        if u.get("role_id") != 3:
            continue
        rows_out.append(
            EnrollmentRosterRow(
                id=uid,
                full_name=u.get("full_name"),
                email=u.get("email"),
                student_class=class_by_uid.get(uid),
            )
        )
    return rows_out


@router.get(
    "/{course_id}/roster",
    response_model=List[EnrollmentRosterRow],
    summary="Enrolled students with profile fields (course editors only; not full directory)",
)
async def enrollment_roster(
    course_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    crs = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not crs.data:
        raise HTTPException(status_code=404, detail="Course not found")
    if not _can_edit_course(user, crs.data[0]):
        raise HTTPException(status_code=403, detail="Forbidden")
    enr = sb.table("course_enrollments").select("student_id").eq("course_id", course_id).execute()
    ids = [r["student_id"] for r in (enr.data or []) if r.get("student_id")]
    return _roster_rows_for_student_ids(sb, ids)


@router.get(
    "/{course_id}/students-by-class",
    response_model=List[EnrollmentRosterRow],
    summary="Students matching a class label for enrollment (course editors only; scoped, not global directory)",
)
async def students_by_class_for_course(
    course_id: int,
    student_class: str = Query(..., min_length=1, description="Match student_profiles.class"),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    crs = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not crs.data:
        raise HTTPException(status_code=404, detail="Course not found")
    if not _can_edit_course(user, crs.data[0]):
        raise HTTPException(status_code=403, detail="Forbidden")
    token = student_class.strip()
    sp = (
        sb.table("student_profiles")
        .select("user_id")
        .ilike("class", f"%{token}%")
        .execute()
    )
    uids = [r["user_id"] for r in (sp.data or []) if r.get("user_id")]
    if not uids:
        return []
    return _roster_rows_for_student_ids(sb, uids)


@router.get("/{course_id}/students", response_model=List[CourseEnrollmentOut], summary="List enrolled students")
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
