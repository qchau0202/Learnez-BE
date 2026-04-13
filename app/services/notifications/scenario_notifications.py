"""Insert notifications for LMS scenarios (assignments, enrollment, grading, digests)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# --- Scenario keys (stored in notifications.scenario) ---
DROPOUT_RISK_NOTE = "dropout_risk_note"
COURSE_ANNOUNCEMENT = "course_announcement"
ADMIN_DIRECT_MESSAGE = "admin_direct_message"
ASSIGNMENT_PUBLISHED = "assignment_published"
ASSIGNMENT_DUE_DATE_CHANGED = "assignment_due_date_changed"
ASSIGNMENT_DUE_SOON_3D = "assignment_due_soon_3d"
ASSIGNMENT_DUE_SOON_1D = "assignment_due_soon_1d"
ASSIGNMENT_OVERDUE = "assignment_overdue"
SUBMISSION_RECEIVED = "submission_received"
GRADES_RELEASED = "grades_released"
PARTIAL_GRADING_PENDING = "partial_grading_pending"
ENROLLMENT_ADDED = "enrollment_added"
ENROLLMENT_REMOVED = "enrollment_removed"
MATERIAL_UPLOADED = "material_uploaded"
LOW_ATTENDANCE_WARNING = "low_attendance_warning"
DAILY_DIGEST_STUDENT = "daily_digest_student"
WEEKLY_LECTURER_DIGEST = "weekly_lecturer_digest"

MANUAL_SCENARIOS_LECTURER = frozenset({DROPOUT_RISK_NOTE, COURSE_ANNOUNCEMENT})
MANUAL_SCENARIOS_ADMIN_ONLY = frozenset({ADMIN_DIRECT_MESSAGE})
MANUAL_SCENARIOS = MANUAL_SCENARIOS_LECTURER | MANUAL_SCENARIOS_ADMIN_ONLY

FRONTEND_BASE = os.environ.get("FRONTEND_BASE", "").rstrip("/")


def public_path(path: str) -> str:
    p = path if path.startswith("/") else f"/{path}"
    return f"{FRONTEND_BASE}{p}" if FRONTEND_BASE else p


def _safe_notify(label: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception:
        log.exception("notification trigger failed: %s", label)


def parse_iso_datetime(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    s = str(val).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def insert_notification(
    sb,
    *,
    recipient_id: str,
    title: str,
    body: str,
    notification_type: str,
    course_id: int | None = None,
    scenario: str | None = None,
    metadata: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
) -> dict | None:
    if dedupe_key:
        ex = sb.table("notifications").select("id").eq("dedupe_key", dedupe_key).limit(1).execute()
        if ex.data:
            return None
    row: dict[str, Any] = {
        "recipient_id": recipient_id,
        "title": title,
        "body": body,
        "notification_type": notification_type,
        "is_read": False,
        "is_pinned": False,
    }
    if course_id is not None:
        row["course_id"] = course_id
    if scenario is not None:
        row["scenario"] = scenario
    if metadata is not None:
        row["metadata"] = metadata
    if dedupe_key is not None:
        row["dedupe_key"] = dedupe_key
    ins = sb.table("notifications").insert(row).execute()
    return ins.data[0] if ins.data else None


def _module_row(sb, module_id: int | None) -> dict | None:
    if module_id is None:
        return None
    r = sb.table("modules").select("*").eq("id", module_id).limit(1).execute()
    return r.data[0] if r.data else None


def _course_row(sb, course_id: int | None) -> dict | None:
    if course_id is None:
        return None
    r = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    return r.data[0] if r.data else None


def assignment_course_id(sb, assignment_row: dict) -> int | None:
    mod = _module_row(sb, assignment_row.get("module_id"))
    return mod.get("course_id") if mod else None


def enrolled_student_ids(sb, course_id: int) -> list[str]:
    r = sb.table("course_enrollments").select("student_id").eq("course_id", course_id).execute()
    return [x["student_id"] for x in (r.data or [])]


def student_has_submitted(sb, assignment_id: int, student_id: str) -> bool:
    r = (
        sb.table("assignment_submissions")
        .select("id,status")
        .eq("assignment_id", assignment_id)
        .eq("student_id", student_id)
        .limit(1)
        .execute()
    )
    if not r.data:
        return False
    return (r.data[0].get("status") or "").lower() == "submitted"


# --- Event hooks ---


def notify_enrollment_added(sb, *, student_id: str, course_id: int) -> None:
    def _():
        c = _course_row(sb, course_id)
        title = c.get("title") if c else "a course"
        path = public_path(f"/courses/{course_id}/overview")
        insert_notification(
            sb,
            recipient_id=student_id,
            title="Enrolled in course",
            body=f"You have been enrolled in {title}. Open: {path}",
            notification_type="course",
            course_id=course_id,
            scenario=ENROLLMENT_ADDED,
            metadata={"course_id": course_id, "action_path": f"/courses/{course_id}/overview"},
        )

    _safe_notify("enrollment_added", _)


def notify_enrollment_removed(sb, *, student_id: str, course_id: int) -> None:
    def _():
        c = _course_row(sb, course_id)
        title = c.get("title") if c else "a course"
        insert_notification(
            sb,
            recipient_id=student_id,
            title="Removed from course",
            body=f"You have been unenrolled from {title}.",
            notification_type="course",
            course_id=course_id,
            scenario=ENROLLMENT_REMOVED,
            metadata={"course_id": course_id},
        )

    _safe_notify("enrollment_removed", _)


def notify_assignment_published(sb, assignment_row: dict) -> None:
    def _():
        cid = assignment_course_id(sb, assignment_row)
        if cid is None:
            return
        aid = assignment_row["id"]
        title = assignment_row.get("title") or "Assignment"
        students = enrolled_student_ids(sb, cid)
        c = _course_row(sb, cid)
        ctitle = c.get("title") if c else ""
        path = public_path(f"/courses/{cid}/assignments")
        for sid in students:
            insert_notification(
                sb,
                recipient_id=sid,
                title=f"New assignment: {title}",
                body=f"{ctitle}: {title} is available. Due: {assignment_row.get('due_date') or 'TBA'}. Open: {path}",
                notification_type="course",
                course_id=cid,
                scenario=ASSIGNMENT_PUBLISHED,
                metadata={
                    "assignment_id": aid,
                    "course_id": cid,
                    "action_path": f"/courses/{cid}/assignments",
                },
            )

    _safe_notify("assignment_published", _)


def notify_assignment_due_date_changed(
    sb,
    *,
    assignment_row: dict,
    old_due: Any,
    new_due: Any,
) -> None:
    def _():
        cid = assignment_course_id(sb, assignment_row)
        if cid is None:
            return
        aid = assignment_row["id"]
        title = assignment_row.get("title") or "Assignment"
        students = enrolled_student_ids(sb, cid)
        path = public_path(f"/courses/{cid}/assignments")
        for sid in students:
            insert_notification(
                sb,
                recipient_id=sid,
                title=f"Due date updated: {title}",
                body=f"The due date changed from {old_due} to {new_due}. Open: {path}",
                notification_type="reminder",
                course_id=cid,
                scenario=ASSIGNMENT_DUE_DATE_CHANGED,
                metadata={
                    "assignment_id": aid,
                    "course_id": cid,
                    "old_due_date": str(old_due) if old_due is not None else None,
                    "new_due_date": str(new_due) if new_due is not None else None,
                    "action_path": f"/courses/{cid}/assignments",
                },
            )

    _safe_notify("assignment_due_date_changed", _)


def notify_submission_received(
    sb,
    *,
    student_id: str,
    assignment_row: dict,
    submission_id: int,
) -> None:
    def _():
        cid = assignment_course_id(sb, assignment_row)
        aid = assignment_row["id"]
        title = assignment_row.get("title") or "Assignment"
        ap = f"/courses/{cid}/assignments" if cid else "/notifications"
        path = public_path(ap)
        insert_notification(
            sb,
            recipient_id=student_id,
            title=f"Submission received: {title}",
            body=f"We received your submission. Track status here: {path}",
            notification_type="course",
            course_id=cid,
            scenario=SUBMISSION_RECEIVED,
            metadata={
                "assignment_id": aid,
                "submission_id": submission_id,
                "course_id": cid,
                "action_path": ap,
            },
        )

    _safe_notify("submission_received", _)


def notify_partial_grading_pending(
    sb,
    *,
    student_id: str,
    assignment_row: dict,
    submission_id: int,
) -> None:
    def _():
        cid = assignment_course_id(sb, assignment_row)
        aid = assignment_row["id"]
        title = assignment_row.get("title") or "Assignment"
        path = public_path(f"/assignments/take/{aid}/essay")
        insert_notification(
            sb,
            recipient_id=student_id,
            title=f"Grading in progress: {title}",
            body=f"Part of your work is graded; some questions need manual review. Check: {path}",
            notification_type="course",
            course_id=cid,
            scenario=PARTIAL_GRADING_PENDING,
            metadata={
                "assignment_id": aid,
                "submission_id": submission_id,
                "course_id": cid,
                "action_path": f"/assignments/take/{aid}/essay",
            },
        )

    _safe_notify("partial_grading_pending", _)


def notify_grades_released(
    sb,
    *,
    student_id: str,
    assignment_row: dict,
    submission_id: int,
    final_score: float | None,
) -> None:
    def _():
        cid = assignment_course_id(sb, assignment_row)
        aid = assignment_row["id"]
        title = assignment_row.get("title") or "Assignment"
        score_txt = f" Score: {final_score}." if final_score is not None else ""
        path = public_path(f"/courses/{cid}/assignments") if cid else public_path("/notifications")
        insert_notification(
            sb,
            recipient_id=student_id,
            title=f"Grades released: {title}",
            body=f"Your grade is available.{score_txt} View: {path}",
            notification_type="course",
            course_id=cid,
            scenario=GRADES_RELEASED,
            metadata={
                "assignment_id": aid,
                "submission_id": submission_id,
                "course_id": cid,
                "final_score": final_score,
                "action_path": f"/courses/{cid}/assignments" if cid else "/notifications",
            },
        )

    _safe_notify("grades_released", _)


def notify_material_uploaded(
    sb,
    *,
    module_id: int,
    course_id: int,
    material_id: int,
    material_label: str,
) -> None:
    def _():
        students = enrolled_student_ids(sb, course_id)
        c = _course_row(sb, course_id)
        ctitle = c.get("title") if c else ""
        path = public_path(f"/courses/{course_id}/materials")
        for sid in students:
            insert_notification(
                sb,
                recipient_id=sid,
                title="New material uploaded",
                body=f"{ctitle}: {material_label} was added. Open: {path}",
                notification_type="course",
                course_id=course_id,
                scenario=MATERIAL_UPLOADED,
                metadata={
                    "module_id": module_id,
                    "material_id": material_id,
                    "course_id": course_id,
                    "action_path": f"/courses/{course_id}/materials",
                },
            )

    _safe_notify("material_uploaded", _)


# --- Jobs (called from API or cron) ---


def run_due_and_overdue_reminders(sb) -> dict[str, int]:
    """Create due-soon / overdue notifications (deduped)."""
    counts = {"due_3d": 0, "due_1d": 0, "overdue": 0}
    now = datetime.now(timezone.utc)
    res = sb.table("assignments").select("id,module_id,title,due_date").execute()
    for a in res.data or []:
        due = parse_iso_datetime(a.get("due_date"))
        if due is None:
            continue
        cid = assignment_course_id(sb, a)
        if cid is None:
            continue
        aid = a["id"]
        title = a.get("title") or "Assignment"
        students = enrolled_student_ids(sb, cid)
        path = public_path(f"/courses/{cid}/assignments")
        delta = due - now
        days_left = delta.total_seconds() / 86400.0

        for sid in students:
            if student_has_submitted(sb, aid, sid):
                continue

            if days_left < 0:
                dk = f"overdue:{aid}:{sid}"
                if insert_notification(
                    sb,
                    recipient_id=sid,
                    title=f"Overdue: {title}",
                    body=f"This assignment is past due. Submit as soon as possible. Open: {path}",
                    notification_type="reminder",
                    course_id=cid,
                    scenario=ASSIGNMENT_OVERDUE,
                    metadata={"assignment_id": aid, "course_id": cid, "action_path": f"/courses/{cid}/assignments"},
                    dedupe_key=dk,
                ):
                    counts["overdue"] += 1
            elif 0 < days_left <= 1:
                dk = f"due1d:{aid}:{sid}"
                if insert_notification(
                    sb,
                    recipient_id=sid,
                    title=f"Due within 24h: {title}",
                    body=f"Assignment is due soon ({due.isoformat()}). Open: {path}",
                    notification_type="reminder",
                    course_id=cid,
                    scenario=ASSIGNMENT_DUE_SOON_1D,
                    metadata={"assignment_id": aid, "course_id": cid, "due_at": due.isoformat()},
                    dedupe_key=dk,
                ):
                    counts["due_1d"] += 1
            elif 1 < days_left <= 3:
                dk = f"due3d:{aid}:{sid}"
                if insert_notification(
                    sb,
                    recipient_id=sid,
                    title=f"Due in ~3 days: {title}",
                    body=f"Reminder: due {due.isoformat()}. Open: {path}",
                    notification_type="reminder",
                    course_id=cid,
                    scenario=ASSIGNMENT_DUE_SOON_3D,
                    metadata={"assignment_id": aid, "course_id": cid, "due_at": due.isoformat()},
                    dedupe_key=dk,
                ):
                    counts["due_3d"] += 1
    return counts


def run_daily_student_digests(sb) -> int:
    """One digest per enrolled student: assignments due in the next 7 days."""
    horizon = datetime.now(timezone.utc) + timedelta(days=7)
    enr = sb.table("course_enrollments").select("student_id,course_id").execute()
    by_student: dict[str, list[tuple[int, str, datetime]]] = {}
    for row in enr.data or []:
        sid = row.get("student_id")
        cid = row.get("course_id")
        if not sid or cid is None:
            continue
        mods = sb.table("modules").select("id").eq("course_id", cid).execute()
        mids = [m["id"] for m in (mods.data or [])]
        if not mids:
            continue
        asg = sb.table("assignments").select("id,title,due_date,module_id").in_("module_id", mids).execute()
        for a in asg.data or []:
            due = parse_iso_datetime(a.get("due_date"))
            if due is None or due > horizon or due < datetime.now(timezone.utc):
                continue
            if student_has_submitted(sb, a["id"], sid):
                continue
            by_student.setdefault(sid, []).append((cid, a.get("title") or "Assignment", due))

    n = 0
    for sid, items in by_student.items():
        if not items:
            continue
        lines = [f"- {t} (course {cid}, due {d.isoformat()})" for cid, t, d in sorted(items, key=lambda x: x[2])]
        body = "Upcoming deadlines (7 days):\n" + "\n".join(lines)
        if insert_notification(
            sb,
            recipient_id=sid,
            title="Daily digest: upcoming assignments",
            body=body[:4900],
            notification_type="reminder",
            course_id=items[0][0],
            scenario=DAILY_DIGEST_STUDENT,
            metadata={"window_days": 7, "count": len(items)},
        ):
            n += 1
    return n


def run_weekly_lecturer_digests(sb) -> int:
    """Simple weekly summary per lecturer (courses they teach)."""
    crs = sb.table("courses").select("id,title,lecturer_id").execute()
    by_lec: dict[str, list[dict]] = {}
    for c in crs.data or []:
        lec = c.get("lecturer_id")
        if not lec:
            continue
        by_lec.setdefault(lec, []).append(c)

    n = 0
    for lec_id, courses in by_lec.items():
        parts = []
        for c in courses:
            cid = c["id"]
            ec = sb.table("course_enrollments").select("student_id").eq("course_id", cid).execute()
            cnt = len(ec.data or [])
            parts.append(f"- {c.get('title')}: {cnt} students")
        body = "Weekly teaching digest:\n" + "\n".join(parts) if parts else "No courses assigned."
        if insert_notification(
            sb,
            recipient_id=lec_id,
            title="Weekly digest: your courses",
            body=body[:4900],
            notification_type="system",
            course_id=courses[0]["id"] if courses else None,
            scenario=WEEKLY_LECTURER_DIGEST,
            metadata={"course_count": len(courses)},
        ):
            n += 1
    return n


def demo_low_attendance_warning(
    sb,
    *,
    student_id: str,
    course_id: int,
    note: str = "Attendance has dropped below the expected threshold for this course.",
) -> Optional[dict]:
    """Placeholder until attendance is backed by Supabase; used for E2E / demos."""
    return insert_notification(
        sb,
        recipient_id=student_id,
        title="Attendance alert",
        body=note,
        notification_type="system",
        course_id=course_id,
        scenario=LOW_ATTENDANCE_WARNING,
        metadata={"course_id": course_id, "source": "demo_or_batch"},
    )
