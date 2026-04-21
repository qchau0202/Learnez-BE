"""Attendance endpoints — CRUD over public.course_attendance.

Statuses:
- `Present` — student attended the session.
- `Late`    — student attended but arrived after session start.
- `Absent`  — student did not attend.

Access model:
- Admin can read / write attendance for any course.
- Lecturer can read / write attendance for courses they own (`courses.lecturer_id == user_id`).
- Student can read only their own attendance rows, and only for courses they are
  enrolled in.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from postgrest.exceptions import APIError

from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, require_roles
from app.models.attendance import (
    AttendanceBulkUpsertIn,
    AttendanceRecordOut,
    AttendanceSessionOut,
    AttendanceSessionStudentRow,
    AttendanceStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attendance", tags=["Learning - Attendance"])


def _sb():
    supabase = get_supabase(service_role=True)
    if not supabase:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")
    return supabase


def _role(user: dict[str, Any]) -> str | None:
    return ROLE_MAP.get(user.get("role_id"))


def _require_course_row(sb, course_id: int) -> dict:
    res = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Course not found")
    return res.data[0]


def _can_edit_course(user: dict[str, Any], course_row: dict) -> bool:
    role = _role(user)
    if role == "Admin":
        return True
    if role == "Lecturer" and course_row.get("lecturer_id") == user.get("user_id"):
        return True
    return False


def _is_student_enrolled(sb, course_id: int, student_id: str) -> bool:
    try:
        enr = (
            sb.table("course_enrollments")
            .select("course_id")
            .eq("course_id", course_id)
            .eq("student_id", student_id)
            .limit(1)
            .execute()
        )
    except APIError as exc:
        logger.warning("attendance: enrollment check failed: %s", exc)
        return False
    return bool(enr.data)


def _parse_session_date(raw: str) -> date:
    """Accept 'YYYY-MM-DD' or full ISO 8601 — return a `date`."""
    if not raw:
        raise HTTPException(status_code=400, detail="session_date is required")
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid session_date: {exc}") from exc


def _session_date_storage_value(d: date) -> str:
    """Canonical storage value — midnight UTC on the given calendar day."""
    return datetime.combine(d, time(0, 0, 0), tzinfo=timezone.utc).isoformat()


def _session_day_bounds(d: date) -> tuple[str, str]:
    """Inclusive-start, exclusive-end ISO bounds for the calendar day in UTC."""
    start = datetime.combine(d, time(0, 0, 0), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _attendance_records_for_course(
    sb,
    *,
    course_id: int,
    student_id: Optional[str] = None,
    session_day: Optional[date] = None,
) -> list[dict]:
    q = sb.table("course_attendance").select("*").eq("course_id", course_id)
    if student_id:
        q = q.eq("student_id", student_id)
    if session_day is not None:
        start, end = _session_day_bounds(session_day)
        q = q.gte("session_date", start).lt("session_date", end)
    res = q.order("session_date", desc=True).order("id", desc=True).execute()
    return res.data or []


@router.post(
    "/courses/{course_id}/records",
    response_model=list[AttendanceRecordOut],
    status_code=status.HTTP_200_OK,
    summary="Bulk upsert attendance rows for a single session date",
)
async def upsert_attendance(
    course_id: int,
    payload: AttendanceBulkUpsertIn,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    course = _require_course_row(sb, course_id)
    if not _can_edit_course(user, course):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not payload.records:
        return []

    session_day = _parse_session_date(payload.session_date)
    session_iso = _session_date_storage_value(session_day)
    start_iso, end_iso = _session_day_bounds(session_day)
    recorded_by = user.get("user_id")

    existing = (
        sb.table("course_attendance")
        .select("id,student_id")
        .eq("course_id", course_id)
        .gte("session_date", start_iso)
        .lt("session_date", end_iso)
        .execute()
    )
    existing_by_sid: dict[str, int] = {
        str(r["student_id"]): int(r["id"])
        for r in (existing.data or [])
        if r.get("student_id") is not None and r.get("id") is not None
    }

    # Enrollment safety — silently drop any student_id that is not enrolled in
    # this course so we never write stray rows.
    enrolled_res = (
        sb.table("course_enrollments")
        .select("student_id")
        .eq("course_id", course_id)
        .execute()
    )
    enrolled_ids = {str(r["student_id"]) for r in (enrolled_res.data or []) if r.get("student_id")}

    saved_rows: list[dict] = []
    for rec in payload.records:
        sid = str(rec.student_id)
        if sid not in enrolled_ids:
            logger.info(
                "attendance: skipping student %s not enrolled in course %s",
                sid,
                course_id,
            )
            continue
        row_patch = {
            "status": rec.status.value,
            "notes": rec.notes,
            "recorded_by": recorded_by,
        }
        att_id = existing_by_sid.get(sid)
        try:
            if att_id is not None:
                upd = (
                    sb.table("course_attendance")
                    .update(row_patch)
                    .eq("id", att_id)
                    .execute()
                )
                if upd.data:
                    saved_rows.append(upd.data[0])
            else:
                ins = (
                    sb.table("course_attendance")
                    .insert(
                        {
                            **row_patch,
                            "course_id": course_id,
                            "student_id": sid,
                            "session_date": session_iso,
                        }
                    )
                    .execute()
                )
                if ins.data:
                    saved_rows.append(ins.data[0])
        except APIError as exc:
            logger.warning(
                "attendance: failed to upsert row for course=%s student=%s: %s",
                course_id,
                sid,
                exc,
            )
    return saved_rows


@router.get(
    "/courses/{course_id}/session",
    response_model=AttendanceSessionOut,
    summary="Roll-call view for a single session date (lecturer / admin)",
)
async def get_session_attendance(
    course_id: int,
    session_date: str = Query(..., description="Session date (YYYY-MM-DD or full ISO 8601)."),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    course = _require_course_row(sb, course_id)
    if not _can_edit_course(user, course):
        raise HTTPException(status_code=403, detail="Forbidden")

    session_day = _parse_session_date(session_date)

    enr = (
        sb.table("course_enrollments")
        .select("student_id")
        .eq("course_id", course_id)
        .execute()
    )
    student_ids = [str(r["student_id"]) for r in (enr.data or []) if r.get("student_id")]

    users_by_id: dict[str, dict] = {}
    class_by_uid: dict[str, Optional[str]] = {}
    if student_ids:
        try:
            urows = (
                sb.table("users")
                .select("user_id,full_name,email")
                .in_("user_id", student_ids)
                .execute()
            )
            users_by_id = {str(r["user_id"]): r for r in (urows.data or []) if r.get("user_id")}
        except APIError as exc:
            logger.warning("attendance: user profile lookup failed: %s", exc)
        try:
            prof = (
                sb.table("student_profiles")
                .select("user_id,class")
                .in_("user_id", student_ids)
                .execute()
            )
            class_by_uid = {str(r["user_id"]): r.get("class") for r in (prof.data or [])}
        except APIError as exc:
            logger.warning("attendance: student_profiles lookup failed: %s", exc)

    attendance_rows = _attendance_records_for_course(
        sb, course_id=course_id, session_day=session_day
    )
    attendance_by_sid = {str(r.get("student_id")): r for r in attendance_rows if r.get("student_id")}

    rows: list[AttendanceSessionStudentRow] = []
    for sid in student_ids:
        u = users_by_id.get(sid) or {}
        att = attendance_by_sid.get(sid) or {}
        recorded_at_raw = att.get("created_at")
        recorded_at_val: Optional[datetime] = None
        if recorded_at_raw:
            try:
                recorded_at_val = datetime.fromisoformat(str(recorded_at_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                recorded_at_val = None
        rows.append(
            AttendanceSessionStudentRow(
                student_id=sid,
                full_name=u.get("full_name"),
                email=u.get("email"),
                student_class=class_by_uid.get(sid),
                attendance_id=att.get("id"),
                status=att.get("status"),
                notes=att.get("notes"),
                recorded_by=att.get("recorded_by"),
                recorded_at=recorded_at_val,
            )
        )
    rows.sort(key=lambda r: ((r.full_name or "").lower(), r.student_id))
    return AttendanceSessionOut(
        course_id=course_id,
        session_date=session_day.isoformat(),
        records=rows,
    )


@router.get(
    "/courses/{course_id}/records",
    response_model=list[AttendanceRecordOut],
    summary="List attendance rows for a course (lecturer / admin)",
)
async def list_course_attendance(
    course_id: int,
    student_id: Optional[str] = Query(default=None),
    session_date: Optional[str] = Query(default=None),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    course = _require_course_row(sb, course_id)
    if not _can_edit_course(user, course):
        raise HTTPException(status_code=403, detail="Forbidden")
    session_day = _parse_session_date(session_date) if session_date else None
    return _attendance_records_for_course(
        sb,
        course_id=course_id,
        student_id=student_id,
        session_day=session_day,
    )


@router.get(
    "/courses/{course_id}/me",
    response_model=list[AttendanceRecordOut],
    summary="Student: list own attendance for a course (read-only)",
)
async def list_my_course_attendance(
    course_id: int,
    user: dict[str, Any] = Depends(require_roles(["Student", "Admin", "Lecturer"])),
):
    sb = _sb()
    course = _require_course_row(sb, course_id)
    role = _role(user)
    uid = user.get("user_id")

    if role == "Student":
        if not _is_student_enrolled(sb, course_id, uid):
            raise HTTPException(status_code=403, detail="Forbidden")
        target_student = uid
    else:
        # Lecturer/Admin hitting this endpoint while previewing — only return
        # rows if they actually belong to them (no aggregate data leak here).
        if not _can_edit_course(user, course):
            raise HTTPException(status_code=403, detail="Forbidden")
        target_student = uid

    return _attendance_records_for_course(
        sb, course_id=course_id, student_id=target_student
    )


@router.delete(
    "/records/{attendance_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a single attendance record (lecturer / admin)",
)
async def delete_attendance_record(
    attendance_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    row = (
        sb.table("course_attendance")
        .select("id,course_id")
        .eq("id", attendance_id)
        .limit(1)
        .execute()
    )
    if not row.data:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    course_id = row.data[0].get("course_id")
    if course_id is not None:
        course = _require_course_row(sb, int(course_id))
        if not _can_edit_course(user, course):
            raise HTTPException(status_code=403, detail="Forbidden")
    sb.table("course_attendance").delete().eq("id", attendance_id).execute()
    return None
