"""Notifications API — public.notifications (Supabase)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, require_roles
from app.models.notification import (
    DemoLowAttendanceIn,
    NotificationCreate,
    NotificationIdsIn,
    NotificationOut,
    NotificationRecipientUpdate,
    NotificationUpdate,
)
from app.services.notifications.scenario_notifications import (
    MANUAL_SCENARIOS,
    MANUAL_SCENARIOS_ADMIN_ONLY,
    demo_low_attendance_warning,
    run_daily_student_digests,
    run_due_and_overdue_reminders,
    run_weekly_lecturer_digests,
)

router = APIRouter(prefix="/notifications", tags=["Learning - Notifications"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


def _sb():
    supabase = get_supabase(service_role=True)
    if not supabase:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")
    return supabase


def _row(sb, notification_id: int) -> dict | None:
    r = sb.table("notifications").select("*").eq("id", notification_id).limit(1).execute()
    return r.data[0] if r.data else None


def _user_exists(sb, user_id: str) -> bool:
    r = sb.table("users").select("user_id").eq("user_id", user_id).limit(1).execute()
    return bool(r.data)


def _course_exists(sb, course_id: int) -> bool:
    r = sb.table("courses").select("id").eq("id", course_id).limit(1).execute()
    return bool(r.data)


def _lecturer_course_ids(sb, lecturer_uid: str) -> list[int]:
    r = sb.table("courses").select("id").eq("lecturer_id", lecturer_uid).execute()
    return [x["id"] for x in (r.data or [])]


def _recipient_enrolled_in_any_lecturer_course(
    sb, lecturer_uid: str, recipient_id: str, course_id: int | None
) -> bool:
    cids = _lecturer_course_ids(sb, lecturer_uid)
    if not cids:
        return False
    enr = (
        sb.table("course_enrollments")
        .select("course_id")
        .eq("student_id", recipient_id)
        .in_("course_id", cids)
        .execute()
    )
    rows = enr.data or []
    if not rows:
        return False
    if course_id is None:
        return True
    if course_id not in cids:
        return False
    return any(r.get("course_id") == course_id for r in rows)


def _lecturer_owns_notification_course(sb, lecturer_uid: str, notif: dict) -> bool:
    cid = notif.get("course_id")
    if cid is None:
        return False
    c = sb.table("courses").select("lecturer_id").eq("id", cid).limit(1).execute()
    if not c.data:
        return False
    return c.data[0].get("lecturer_id") == lecturer_uid


def _can_view(sb, user: dict[str, Any], notif: dict) -> bool:
    role = ROLE_MAP.get(user.get("role_id"))
    if role == "Admin":
        return True
    if notif.get("recipient_id") == user.get("user_id"):
        return True
    if role == "Lecturer":
        return _lecturer_owns_notification_course(sb, user["user_id"], notif)
    return False


def _can_full_mutate(user: dict[str, Any], sb, notif: dict) -> bool:
    role = ROLE_MAP.get(user.get("role_id"))
    if role == "Admin":
        return True
    if role == "Lecturer":
        return _lecturer_owns_notification_course(sb, user["user_id"], notif)
    return False


@router.get("/", response_model=List[NotificationOut])
async def list_notifications(
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
    recipient_id: Optional[str] = Query(
        None,
        description="Admin: filter by any recipient. Others: only allowed when equal to your user_id (no extra access).",
    ),
    unread_only: bool = Query(False),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    sb = _sb()
    role = ROLE_MAP.get(user["role_id"])
    q = sb.table("notifications").select("*")

    if role != "Admin" and recipient_id and recipient_id != user["user_id"]:
        raise HTTPException(
            status_code=403,
            detail="recipient_id may only target another user when using an Admin account",
        )

    if role == "Admin":
        if recipient_id:
            q = q.eq("recipient_id", recipient_id)
    elif role == "Lecturer":
        uid = user["user_id"]
        cids = _lecturer_course_ids(sb, uid)
        if cids:
            in_list = ",".join(str(c) for c in cids)
            q = q.or_(f"recipient_id.eq.{uid},course_id.in.({in_list})")
        else:
            q = q.eq("recipient_id", uid)
    else:
        q = q.eq("recipient_id", user["user_id"])

    if unread_only:
        q = q.eq("is_read", False)

    res = q.order("created_at", desc=True).range(offset, offset + limit - 1).execute()
    return res.data or []


@router.post("/jobs/due-reminders", response_model=dict)
async def job_due_reminders(user: dict[str, Any] = Depends(require_roles(["Admin"]))):
    """Scan assignments and enqueue due-soon / overdue reminders (deduped)."""
    sb = _sb()
    return run_due_and_overdue_reminders(sb)


@router.post("/jobs/digests", response_model=dict)
async def job_digests(user: dict[str, Any] = Depends(require_roles(["Admin"]))):
    """Run daily student digests and weekly lecturer digests."""
    sb = _sb()
    return {
        "daily_students_notified": run_daily_student_digests(sb),
        "weekly_lecturers_notified": run_weekly_lecturer_digests(sb),
    }


@router.post("/jobs/demo-low-attendance", response_model=NotificationOut)
async def job_demo_low_attendance(
    payload: DemoLowAttendanceIn,
    user: dict[str, Any] = Depends(require_roles(["Admin"])),
):
    """Demo low-attendance notification until attendance is wired to Supabase."""
    sb = _sb()
    row = demo_low_attendance_warning(
        sb,
        student_id=payload.student_id,
        course_id=payload.course_id,
        note=payload.note or "Attendance has dropped below the expected threshold for this course.",
    )
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create notification")
    return row


@router.get("/{notification_id}", response_model=NotificationOut)
async def get_notification(
    notification_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    row = _row(sb, notification_id)
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not _can_view(sb, user, row):
        raise HTTPException(status_code=403, detail="Forbidden")
    return row


@router.post("/", response_model=NotificationOut, status_code=status.HTTP_201_CREATED)
async def create_notification(
    payload: NotificationCreate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    role = ROLE_MAP.get(user["role_id"])

    if not _user_exists(sb, payload.recipient_id):
        raise HTTPException(status_code=400, detail="recipient_id not found in users")

    if payload.course_id is not None:
        if not _course_exists(sb, payload.course_id):
            raise HTTPException(status_code=400, detail="course_id not found")

    if role == "Lecturer":
        if not _recipient_enrolled_in_any_lecturer_course(
            sb, user["user_id"], payload.recipient_id, payload.course_id
        ):
            raise HTTPException(
                status_code=403,
                detail="Recipient must be enrolled in a course you lecture, and course_id (if set) must be your course.",
            )

    if payload.scenario is not None:
        if payload.scenario not in MANUAL_SCENARIOS:
            raise HTTPException(status_code=400, detail="Invalid scenario for manual notification")
        if payload.scenario in MANUAL_SCENARIOS_ADMIN_ONLY and role != "Admin":
            raise HTTPException(status_code=403, detail="This scenario is restricted to Admin")

    ins_row: dict[str, Any] = {
        "recipient_id": payload.recipient_id,
        "title": payload.title,
        "body": payload.body,
        "notification_type": payload.notification_type,
        "course_id": payload.course_id,
        "is_pinned": payload.is_pinned,
        "is_read": False,
    }
    if payload.scenario is not None:
        ins_row["scenario"] = payload.scenario
    if payload.metadata is not None:
        ins_row["metadata"] = payload.metadata

    ins = sb.table("notifications").insert(ins_row).execute()
    if not ins.data:
        raise HTTPException(status_code=500, detail="Failed to create notification")
    return ins.data[0]


@router.put("/{notification_id}", response_model=NotificationOut)
async def update_notification(
    notification_id: int,
    payload: NotificationUpdate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    row = _row(sb, notification_id)
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")

    role = ROLE_MAP.get(user["role_id"])
    is_recipient = row.get("recipient_id") == user.get("user_id")

    if not _can_view(sb, user, row):
        raise HTTPException(status_code=403, detail="Forbidden")

    data = payload.model_dump(exclude_unset=True)

    if is_recipient and role != "Admin" and not _can_full_mutate(user, sb, row):
        allowed = {"is_read", "read_at", "is_pinned"}
        data = {k: v for k, v in data.items() if k in allowed}
        if not data:
            return row
        if data.get("is_read") is True and "read_at" not in data:
            data["read_at"] = datetime.now(timezone.utc).isoformat()
        if data.get("is_read") is False:
            data["read_at"] = None

    elif not _can_full_mutate(user, sb, row):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not data:
        return row

    upd = sb.table("notifications").update(data).eq("id", notification_id).execute()
    if not upd.data:
        raise HTTPException(status_code=500, detail="Failed to update notification")
    return upd.data[0]


@router.patch("/{notification_id}/recipient", response_model=NotificationOut)
async def update_notification_as_recipient(
    notification_id: int,
    payload: NotificationRecipientUpdate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Explicit recipient-only update (read / pin)."""
    sb = _sb()
    row = _row(sb, notification_id)
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    if row.get("recipient_id") != user.get("user_id"):
        raise HTTPException(status_code=403, detail="Forbidden")

    data = payload.model_dump(exclude_unset=True)
    if not data:
        return row
    if data.get("is_read") is True and "read_at" not in data:
        data["read_at"] = datetime.now(timezone.utc).isoformat()
    if data.get("is_read") is False:
        data["read_at"] = None

    upd = sb.table("notifications").update(data).eq("id", notification_id).execute()
    if not upd.data:
        raise HTTPException(status_code=500, detail="Failed to update notification")
    return upd.data[0]


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification(
    notification_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    row = _row(sb, notification_id)
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")

    is_recipient = row.get("recipient_id") == user.get("user_id")
    if is_recipient or ROLE_MAP.get(user["role_id"]) == "Admin" or _can_full_mutate(user, sb, row):
        sb.table("notifications").delete().eq("id", notification_id).execute()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    raise HTTPException(status_code=403, detail="Forbidden")


def _bulk_ids_owned_by_recipient(sb, user_id: str, ids: list[int]) -> list[int]:
    if not ids:
        return []
    res = (
        sb.table("notifications")
        .select("id")
        .eq("recipient_id", user_id)
        .in_("id", ids)
        .execute()
    )
    return [r["id"] for r in (res.data or [])]


def _bulk_ids_lecturer_may_delete(sb, lecturer_uid: str, ids: list[int]) -> list[int]:
    """Recipient-owned or notifications for a course this lecturer teaches."""
    if not ids:
        return []
    res = sb.table("notifications").select("id,course_id,recipient_id").in_("id", ids).execute()
    owned_courses = set(_lecturer_course_ids(sb, lecturer_uid))
    out: list[int] = []
    for r in res.data or []:
        if r.get("recipient_id") == lecturer_uid:
            out.append(r["id"])
        elif r.get("course_id") is not None and r.get("course_id") in owned_courses:
            out.append(r["id"])
    return out


@router.post("/bulk/mark-read", response_model=dict)
async def bulk_mark_read(
    body: NotificationIdsIn,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    role = ROLE_MAP.get(user["role_id"])
    now_iso = datetime.now(timezone.utc).isoformat()

    if role == "Admin":
        sb.table("notifications").update({"is_read": True, "read_at": now_iso}).in_("id", body.ids).execute()
        return {"updated": len(body.ids)}

    owned = _bulk_ids_owned_by_recipient(sb, user["user_id"], body.ids)
    if owned:
        sb.table("notifications").update({"is_read": True, "read_at": now_iso}).in_("id", owned).execute()
    return {"updated": len(owned)}


@router.post("/bulk/mark-unread", response_model=dict)
async def bulk_mark_unread(
    body: NotificationIdsIn,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    role = ROLE_MAP.get(user["role_id"])
    if role == "Admin":
        sb.table("notifications").update({"is_read": False, "read_at": None}).in_("id", body.ids).execute()
        return {"updated": len(body.ids)}
    owned = _bulk_ids_owned_by_recipient(sb, user["user_id"], body.ids)
    if owned:
        sb.table("notifications").update({"is_read": False, "read_at": None}).in_("id", owned).execute()
    return {"updated": len(owned)}


@router.post("/bulk/delete", response_model=dict)
async def bulk_delete(
    body: NotificationIdsIn,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    role = ROLE_MAP.get(user["role_id"])
    if role == "Admin":
        sb.table("notifications").delete().in_("id", body.ids).execute()
        return {"deleted": len(body.ids)}

    if role == "Lecturer":
        allowed = _bulk_ids_lecturer_may_delete(sb, user["user_id"], body.ids)
        if allowed:
            sb.table("notifications").delete().in_("id", allowed).execute()
        return {"deleted": len(allowed)}

    owned = _bulk_ids_owned_by_recipient(sb, user["user_id"], body.ids)
    if owned:
        sb.table("notifications").delete().in_("id", owned).execute()
    return {"deleted": len(owned)}
