"""Course CRUD — aligned with public.courses (Supabase)."""

import io
import logging
from datetime import date as _date_type, datetime as _datetime_type, timedelta
from typing import Any, List

import pandas as pd

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status

from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, require_permissions, require_roles
from app.models.course import (
    CourseAdminManagementOut,
    CourseCreate,
    CourseOut,
    CourseUpdate,
    ModuleCreate,
    ModuleOut,
    ModuleUpdate,
)
from app.services.assignment_cascade import delete_assignment_cascade

router = APIRouter(prefix="/courses", tags=["Course & Content - Courses"])


def _sb():
    supabase = get_supabase(service_role=True)
    if not supabase:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")
    return supabase


def _can_view_course(user: dict[str, Any], course_row: dict) -> bool:
    role = ROLE_MAP.get(user.get("role_id"))
    uid = user.get("user_id")
    if role == "Admin":
        return True
    if role == "Lecturer" and course_row.get("lecturer_id") == uid:
        return True
    if role == "Student":
        sb = _sb()
        enr = (
            sb.table("course_enrollments")
            .select("course_id")
            .eq("course_id", course_row["id"])
            .eq("student_id", uid)
            .limit(1)
            .execute()
        )
        return bool(enr.data)
    return False


def _can_edit_course(user: dict[str, Any], course_row: dict) -> bool:
    role = ROLE_MAP.get(user.get("role_id"))
    uid = user.get("user_id")
    if role == "Admin":
        return True
    if role == "Lecturer" and course_row.get("lecturer_id") == uid:
        return True
    return False


def _derive_course_end_date(start_date: Any, occurences: Any) -> str | None:
    if start_date is None or occurences is None:
        return None
    try:
        occ = int(occurences)
        if occ < 1:
            return None
        if hasattr(start_date, "isoformat"):
            base = start_date
        else:
            # Expecting "YYYY-MM-DD"
            from datetime import date as _date
            base = _date.fromisoformat(str(start_date))
        return (base + timedelta(days=7 * (occ - 1))).isoformat()
    except Exception:
        return None


@router.post("/", response_model=CourseOut, status_code=status.HTTP_201_CREATED, summary="Create course")
async def create_course(
    payload: CourseCreate,
    user: dict[str, Any] = Depends(require_permissions(["course-01"])),
):
    sb = _sb()
    row = {
        "title": payload.title,
        "description": payload.description or "",
        "course_code": payload.course_code,
        "semester": payload.semester,
        "academic_year": payload.academic_year,
        "class_room": payload.class_room,
        "course_occurences": payload.course_occurences,
        "course_session": payload.course_session,
        "course_session_date": payload.course_session_date,
        "course_session_duration": payload.course_session_duration,
        "course_start_date": payload.course_start_date.isoformat() if payload.course_start_date else None,
        "course_end_date": payload.course_end_date.isoformat() if payload.course_end_date else None,
        "lecturer_id": payload.lecturer_id,
        "from_department": payload.from_department,
        "schedule": payload.schedule.isoformat() if payload.schedule else None,
        "is_complete": payload.is_complete,
        "created_by": user["user_id"],
    }
    if row.get("course_end_date") is None:
        row["course_end_date"] = _derive_course_end_date(
            payload.course_start_date, payload.course_occurences
        )
    row = {k: v for k, v in row.items() if v is not None}
    created = sb.table("courses").insert(row).execute()
    if not created.data:
        raise HTTPException(status_code=500, detail="Failed to create course")
    return created.data[0]


def _enrich_courses_for_list(sb, courses: list[dict]) -> list[dict]:
    """Attach `lecturer_name` and `student_count` fields to each course row.

    Failures on the auxiliary queries must never break the list endpoint — we
    degrade gracefully so the UI can still render the core course data.
    """
    if not courses:
        return []

    lecturer_ids = sorted({c.get("lecturer_id") for c in courses if c.get("lecturer_id")})
    name_by_uid: dict[str, str | None] = {}
    if lecturer_ids:
        try:
            users_res = (
                sb.table("users")
                .select("user_id,full_name")
                .in_("user_id", lecturer_ids)
                .execute()
            )
            for r in users_res.data or []:
                name_by_uid[str(r.get("user_id"))] = r.get("full_name")
        except Exception as exc:
            logger.warning("list_courses: could not resolve lecturer names: %s", exc)

    course_ids = [c.get("id") for c in courses if c.get("id") is not None]
    count_by_cid: dict[int, int] = {}
    if course_ids:
        try:
            enr_res = (
                sb.table("course_enrollments")
                .select("course_id")
                .in_("course_id", course_ids)
                .execute()
            )
            for r in enr_res.data or []:
                cid = r.get("course_id")
                if cid is None:
                    continue
                count_by_cid[cid] = count_by_cid.get(cid, 0) + 1
        except Exception as exc:
            logger.warning("list_courses: could not compute enrollment counts: %s", exc)

    enriched: list[dict] = []
    for c in courses:
        row = dict(c)
        lid = row.get("lecturer_id")
        row["lecturer_name"] = name_by_uid.get(str(lid)) if lid else None
        row["student_count"] = count_by_cid.get(row.get("id"), 0)
        enriched.append(row)
    return enriched


@router.get("/", response_model=List[CourseOut], summary="List courses by role visibility")
async def list_courses(user: dict[str, Any] = Depends(require_permissions(["course-02"]))):
    sb = _sb()
    role = ROLE_MAP.get(user["role_id"])
    uid = user["user_id"]

    if role == "Admin":
        res = sb.table("courses").select("*").order("id", desc=True).execute()
        return _enrich_courses_for_list(sb, res.data or [])

    if role == "Lecturer":
        res = (
            sb.table("courses")
            .select("*")
            .eq("lecturer_id", uid)
            .order("id", desc=True)
            .execute()
        )
        return _enrich_courses_for_list(sb, res.data or [])

    enr = sb.table("course_enrollments").select("course_id").eq("student_id", uid).execute()
    ids = [r["course_id"] for r in (enr.data or [])]
    if not ids:
        return []
    res = sb.table("courses").select("*").in_("id", ids).order("id", desc=True).execute()
    return _enrich_courses_for_list(sb, res.data or [])


@router.get(
    "/admin/management-data",
    response_model=CourseAdminManagementOut,
    summary="Admin course management data in one API",
)
async def admin_course_management_data(
    search: str | None = Query(default=None),
    status: str | None = Query(default=None, description="all|active|completed"),
    semester: str | None = Query(default=None),
    academic_year: str | None = Query(default=None),
    lecturer_id: str | None = Query(default=None),
    faculty_id: int | None = Query(default=None),
    department_id: int | None = Query(default=None),
    class_room: str | None = Query(default=None, description="Filter by class_room / section (partial match)"),
    user: dict[str, Any] = Depends(require_roles(["Admin"])),
):
    sb = _sb()
    course_query = sb.table("courses").select("*")
    if search and search.strip():
        token = search.strip()
        course_query = course_query.or_(
            f"title.ilike.%{token}%,course_code.ilike.%{token}%,class_room.ilike.%{token}%"
        )
    if status and status != "all":
        if status == "completed":
            course_query = course_query.eq("is_complete", True)
        elif status == "active":
            course_query = course_query.eq("is_complete", False)
    if semester and semester != "all":
        # Semester is numeric in DB; accept both int and numeric-string inputs.
        try:
            course_query = course_query.eq("semester", int(semester))
        except (TypeError, ValueError):
            course_query = course_query.eq("semester", semester)
    if academic_year and academic_year != "all":
        course_query = course_query.eq("academic_year", academic_year)
    if lecturer_id and lecturer_id != "all":
        course_query = course_query.eq("lecturer_id", lecturer_id)
    if class_room and class_room.strip():
        course_query = course_query.ilike("class_room", f"%{class_room.strip()}%")
    courses_res = course_query.order("id", desc=True).execute()
    courses = courses_res.data or []

    enr_res = sb.table("course_enrollments").select("course_id").execute()
    count_map: dict[int, int] = {}
    for row in enr_res.data or []:
        cid = row.get("course_id")
        if cid is None:
            continue
        count_map[cid] = count_map.get(cid, 0) + 1

    users_res = (
        sb.table("users")
        .select("user_id, full_name, email, role_id, is_active")
        .in_("role_id", [2, 3])
        .eq("is_active", True)
        .execute()
    )
    users = users_res.data or []

    fac_res = sb.table("faculties").select("id,name").execute()
    faculty_name_by_id = {r["id"]: r.get("name") for r in (fac_res.data or [])}
    dep_res = sb.table("departments").select("id,name,from_faculty").execute()
    dep_by_id = {r["id"]: r for r in (dep_res.data or [])}
    lecturer_ids = [u["user_id"] for u in users if u.get("role_id") == 2]
    lp_by_user_id: dict[str, dict] = {}
    if lecturer_ids:
        lp_res = (
            sb.table("lecturer_profiles")
            .select("user_id,lecturer_id,faculty_id,department_id")
            .in_("user_id", lecturer_ids)
            .execute()
        )
        lp_by_user_id = {r["user_id"]: r for r in (lp_res.data or [])}

    lecturers = [
        {
            "id": u["user_id"],
            "full_name": u.get("full_name"),
            "email": u.get("email"),
            "lecturer_id": lp_by_user_id.get(u["user_id"], {}).get("lecturer_id"),
            "faculty_id": (
                dep_by_id.get(lp_by_user_id.get(u["user_id"], {}).get("department_id"), {}).get("from_faculty")
                or lp_by_user_id.get(u["user_id"], {}).get("faculty_id")
            ),
            "faculty_name": faculty_name_by_id.get(
                dep_by_id.get(lp_by_user_id.get(u["user_id"], {}).get("department_id"), {}).get("from_faculty")
                or lp_by_user_id.get(u["user_id"], {}).get("faculty_id")
            ),
            "department_id": lp_by_user_id.get(u["user_id"], {}).get("department_id"),
            "department_name": dep_by_id.get(lp_by_user_id.get(u["user_id"], {}).get("department_id"), {}).get("name"),
        }
        for u in users
        if u.get("role_id") == 2
    ]
    student_user_ids = [u["user_id"] for u in users if u.get("role_id") == 3]
    student_class_by_uid: dict[str, str | None] = {}
    if student_user_ids:
        spr = (
            sb.table("student_profiles")
            .select("user_id,class")
            .in_("user_id", student_user_ids)
            .execute()
        )
        for r in spr.data or []:
            uid = r.get("user_id")
            if uid:
                student_class_by_uid[str(uid)] = r.get("class")
    students = [
        {
            "id": u["user_id"],
            "full_name": u.get("full_name"),
            "email": u.get("email"),
            "student_class": student_class_by_uid.get(str(u["user_id"])),
        }
        for u in users
        if u.get("role_id") == 3
    ]
    lecturer_meta_by_id = {
        l["id"]: {
            "faculty_id": l.get("faculty_id"),
            "faculty_name": l.get("faculty_name"),
            "department_id": l.get("department_id"),
            "department_name": l.get("department_name"),
        }
        for l in lecturers
    }
    course_items = []
    for c in courses:
        # Prefer the course-owned department (courses.from_department) and derive
        # faculty from departments.from_faculty. Fall back to the lecturer profile
        # only when the course row has no department assigned (legacy data).
        course_dep_id = c.get("from_department")
        resolved_faculty_id = None
        resolved_faculty_name = None
        resolved_department_id = course_dep_id
        resolved_department_name = None
        if course_dep_id is not None:
            dep = dep_by_id.get(course_dep_id, {})
            resolved_faculty_id = dep.get("from_faculty")
            resolved_faculty_name = faculty_name_by_id.get(resolved_faculty_id) if resolved_faculty_id else None
            resolved_department_name = dep.get("name")
        else:
            lmeta = lecturer_meta_by_id.get(c.get("lecturer_id"), {})
            resolved_faculty_id = lmeta.get("faculty_id")
            resolved_faculty_name = lmeta.get("faculty_name")
            resolved_department_id = lmeta.get("department_id")
            resolved_department_name = lmeta.get("department_name")

        row = {
            **c,
            "student_count": count_map.get(c["id"], 0),
            "faculty_id": resolved_faculty_id,
            "faculty_name": resolved_faculty_name,
            "department_id": resolved_department_id,
            "department_name": resolved_department_name,
        }
        if faculty_id is not None and row.get("faculty_id") != faculty_id:
            continue
        if department_id is not None and row.get("department_id") != department_id:
            continue
        course_items.append(row)
    return {"courses": course_items, "lecturers": lecturers, "students": students}


@router.get("/{course_id}", response_model=CourseOut, summary="Get course by id")
async def get_course(
    course_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    res = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Course not found")
    row = res.data[0]
    if not _can_view_course(user, row):
        raise HTTPException(status_code=403, detail="Forbidden")
    out = dict(row)
    # Enrollment count is a separate query; Supabase/PostgREST can occasionally return 502 HTML
    # (gateway) instead of JSON — do not fail the whole GET.
    try:
        enr = sb.table("course_enrollments").select("student_id").eq("course_id", course_id).execute()
        out["student_count"] = len(enr.data or [])
    except Exception as exc:
        logger.warning(
            "get_course: could not load enrollment count for course_id=%s: %s",
            course_id,
            exc,
        )
        out["student_count"] = None
    lid = out.get("lecturer_id")
    if lid:
        try:
            lu = (
                sb.table("users")
                .select("full_name")
                .eq("user_id", lid)
                .limit(1)
                .execute()
            )
            out["lecturer_name"] = (lu.data or [{}])[0].get("full_name")
        except Exception as exc:
            logger.warning(
                "get_course: could not resolve lecturer name for course_id=%s: %s",
                course_id,
                exc,
            )
            out["lecturer_name"] = None
    return out


@router.put("/{course_id}", response_model=CourseOut, summary="Update course")
async def update_course(
    course_id: int,
    payload: CourseUpdate,
    user: dict[str, Any] = Depends(require_permissions(["course-03"])),
):
    sb = _sb()
    res = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Course not found")
    row = res.data[0]
    if not _can_edit_course(user, row):
        raise HTTPException(status_code=403, detail="Forbidden")

    data = payload.model_dump(exclude_unset=True)
    if ROLE_MAP.get(user["role_id"]) == "Lecturer":
        data.pop("lecturer_id", None)
        data.pop("created_by", None)
        # Faculty/department assignment is an admin-only operation.
        data.pop("from_department", None)
    if "schedule" in data and data["schedule"] is not None:
        data["schedule"] = data["schedule"].isoformat()
    if "course_start_date" in data and data["course_start_date"] is not None:
        data["course_start_date"] = data["course_start_date"].isoformat()
    if "course_end_date" in data and data["course_end_date"] is not None:
        data["course_end_date"] = data["course_end_date"].isoformat()
    if "course_end_date" not in data:
        # Compute from new payload values if present, otherwise current row values.
        start_date = data.get("course_start_date", row.get("course_start_date"))
        occ = data.get("course_occurences", row.get("course_occurences"))
        derived = _derive_course_end_date(start_date, occ)
        if derived:
            data["course_end_date"] = derived
    if not data:
        return row

    updated = sb.table("courses").update(data).eq("id", course_id).execute()
    if not updated.data:
        raise HTTPException(status_code=500, detail="Failed to update course")
    return updated.data[0]


@router.delete("/{course_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete course")
async def delete_course(
    course_id: int,
    user: dict[str, Any] = Depends(require_permissions(["course-04"])),
):
    sb = _sb()
    res = sb.table("courses").select("id").eq("id", course_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Course not found")
    sb.table("courses").delete().eq("id", course_id).execute()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Bulk import (CSV / XLSX)
# --------------------------------------------------------------------------- #


def _coerce_str(value: Any) -> str | None:
    """Strip whitespace and return ``None`` for empty / NaN-like values."""
    if value is None:
        return None
    if isinstance(value, float):
        # pandas may pass NaN through fillna("") only for object columns; numeric
        # NaNs survive. Check explicitly to keep import resilient.
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def _coerce_int(value: Any) -> int | None:
    raw = _coerce_str(value)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid integer value: {value}")


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    raw = _coerce_str(value)
    if raw is None:
        return default
    return raw.lower() in {"true", "1", "yes", "y", "t"}


def _coerce_date_iso(value: Any) -> str | None:
    raw = _coerce_str(value)
    if raw is None:
        return None
    if isinstance(value, _datetime_type):
        return value.date().isoformat()
    if isinstance(value, _date_type):
        return value.isoformat()
    # Accept "YYYY-MM-DD" and ISO datetimes.
    try:
        return _date_type.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        # Fall through to pandas — it handles "2026/09/01" etc.
        try:
            ts = pd.to_datetime(raw, errors="raise")
            return ts.date().isoformat()
        except Exception:
            raise HTTPException(
                status_code=400, detail=f"Invalid date value (expected YYYY-MM-DD): {value}"
            )


@router.post(
    "/import",
    status_code=status.HTTP_200_OK,
    summary="Bulk import courses via CSV/XLSX",
)
async def import_courses(
    sheet: UploadFile = File(..., description="CSV/XLSX with course columns"),
    user: dict[str, Any] = Depends(require_roles(["Admin"])),
):
    """Bulk-create courses from a spreadsheet.

    Required column: ``title``. All other columns map to the same fields as
    ``POST /courses``. For convenience the sheet also accepts:

    * ``lecturer_email`` — resolved against ``users.email`` to fill
      ``lecturer_id`` when only the email is known to the operator.
    * ``department_name`` (+ optional ``faculty_name``) — resolved against
      ``departments.name`` to fill ``from_department``.

    Returns ``{total, created, failed, errors[]}`` (mirrors the user
    importer) so the front-end can show one summary toast.
    """
    filename = (sheet.filename or "").lower()
    try:
        raw = await sheet.read()
        if filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(raw))
        elif filename.endswith(".xlsx") or filename.endswith(".xls"):
            df = pd.read_excel(io.BytesIO(raw))
        else:
            raise HTTPException(status_code=400, detail="Only .csv, .xlsx, .xls are supported")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid sheet file: {e}")

    if "title" not in df.columns:
        raise HTTPException(status_code=400, detail="Missing required column: title")

    sb = _sb()

    # Pre-load lookup tables once so we don't hit Supabase per row.
    dept_by_name: dict[tuple[str, str | None], int] = {}
    try:
        dep_rows = (
            sb.table("departments")
            .select("id, name, from_faculty")
            .execute()
            .data
            or []
        )
        fac_rows = (
            sb.table("faculties").select("id, name").execute().data or []
        )
        fac_name_by_id = {int(f["id"]): str(f.get("name") or "") for f in fac_rows}
        for d in dep_rows:
            if d.get("id") is None or not d.get("name"):
                continue
            dname = str(d["name"]).strip().lower()
            fid = d.get("from_faculty")
            fname = (fac_name_by_id.get(int(fid)).lower() if fid is not None else None)
            dept_by_name[(dname, fname)] = int(d["id"])
            # Also index without faculty for fuzzy lookups when the sheet
            # only carries department_name.
            dept_by_name.setdefault((dname, None), int(d["id"]))
    except Exception as exc:
        logger.warning("import_courses: department lookup failed: %s", exc)

    lecturer_id_by_email: dict[str, str] = {}

    def _resolve_lecturer_id(email_or_id: str | None) -> str | None:
        if not email_or_id:
            return None
        if "@" not in email_or_id:
            # Treat as a UUID-style lecturer_id; trust the operator.
            return email_or_id
        email_key = email_or_id.lower()
        if email_key in lecturer_id_by_email:
            return lecturer_id_by_email[email_key]
        try:
            res = (
                sb.table("users")
                .select("user_id, email, role_id")
                .ilike("email", email_or_id)
                .limit(1)
                .execute()
            )
            row = (res.data or [None])[0]
        except Exception as exc:
            logger.warning("import_courses: lecturer lookup failed for %s: %s", email_or_id, exc)
            return None
        if not row:
            return None
        if row.get("role_id") not in (1, 2):
            # Not a staff account — surface the error to the row.
            raise HTTPException(
                status_code=400,
                detail=f"User {email_or_id} is not a lecturer/admin",
            )
        uid = str(row["user_id"])
        lecturer_id_by_email[email_key] = uid
        return uid

    def _resolve_department_id(department_name: str | None, faculty_name: str | None) -> int | None:
        if not department_name:
            return None
        dname = department_name.strip().lower()
        if faculty_name:
            fname = faculty_name.strip().lower()
            if (dname, fname) in dept_by_name:
                return dept_by_name[(dname, fname)]
        return dept_by_name.get((dname, None))

    created = 0
    failed = 0
    errors: list[dict[str, Any]] = []
    inserted_rows: list[dict[str, Any]] = []
    for idx, row in df.fillna("").iterrows():
        try:
            title = _coerce_str(row.get("title"))
            if not title:
                raise HTTPException(status_code=400, detail="Title is required")

            # Resolve lecturer (prefer explicit lecturer_id, fall back to email).
            lecturer_id = _coerce_str(row.get("lecturer_id"))
            if not lecturer_id:
                lecturer_id = _resolve_lecturer_id(_coerce_str(row.get("lecturer_email")))

            # Resolve department (prefer explicit from_department).
            from_department = _coerce_int(row.get("from_department"))
            if from_department is None:
                from_department = _resolve_department_id(
                    _coerce_str(row.get("department_name")),
                    _coerce_str(row.get("faculty_name")),
                )

            payload = {
                "title": title,
                "description": _coerce_str(row.get("description")) or "",
                "course_code": _coerce_str(row.get("course_code")),
                "semester": _coerce_int(row.get("semester")),
                "academic_year": _coerce_str(row.get("academic_year")),
                "class_room": _coerce_str(row.get("class_room")),
                "course_occurences": _coerce_int(row.get("course_occurences")),
                "course_session": _coerce_str(row.get("course_session")),
                "course_session_date": _coerce_str(row.get("course_session_date")),
                "course_session_duration": _coerce_int(row.get("course_session_duration")),
                "course_start_date": _coerce_date_iso(row.get("course_start_date")),
                "course_end_date": _coerce_date_iso(row.get("course_end_date")),
                "lecturer_id": lecturer_id,
                "from_department": from_department,
                "is_complete": _coerce_bool(row.get("is_complete"), default=False),
                "created_by": user["user_id"],
            }

            # Validate semester/duration/occurences ranges (mirror Pydantic limits).
            if payload["semester"] is not None and not (1 <= payload["semester"] <= 4):
                raise HTTPException(status_code=400, detail="semester must be in 1..4")
            if payload["course_occurences"] is not None and not (1 <= payload["course_occurences"] <= 60):
                raise HTTPException(status_code=400, detail="course_occurences must be in 1..60")
            if payload["course_session_duration"] is not None and not (
                0 <= payload["course_session_duration"] <= 1440
            ):
                raise HTTPException(
                    status_code=400, detail="course_session_duration must be in 0..1440"
                )

            if payload["course_end_date"] is None and payload["course_start_date"]:
                derived = _derive_course_end_date(
                    payload["course_start_date"], payload["course_occurences"]
                )
                if derived:
                    payload["course_end_date"] = derived

            insert_row = {k: v for k, v in payload.items() if v is not None}
            res = sb.table("courses").insert(insert_row).execute()
            if not res.data:
                raise HTTPException(status_code=500, detail="Insert returned no row")
            inserted_rows.append(res.data[0])
            created += 1
        except HTTPException as e:
            failed += 1
            errors.append({"row": int(idx) + 2, "error": str(e.detail)})
        except Exception as e:
            failed += 1
            errors.append({"row": int(idx) + 2, "error": str(e)})

    return {
        "total": int(len(df.index)),
        "created": created,
        "failed": failed,
        "errors": errors[:30],
    }


@router.post(
    "/{course_id}/modules",
    response_model=ModuleOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create module in course",
)
async def create_module(
    course_id: int,
    payload: ModuleCreate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    res = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Course not found")
    row = res.data[0]
    if not _can_edit_course(user, row):
        raise HTTPException(status_code=403, detail="Forbidden")
    ins = (
        sb.table("modules")
        .insert(
            {
                "course_id": course_id,
                "title": payload.title,
                "description": payload.description or "",
            }
        )
        .execute()
    )
    if not ins.data:
        raise HTTPException(status_code=500, detail="Failed to create module")
    return ins.data[0]


@router.get("/{course_id}/modules", response_model=List[ModuleOut], summary="List modules in course")
async def list_course_modules(
    course_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    res = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Course not found")
    row = res.data[0]
    if not _can_view_course(user, row):
        raise HTTPException(status_code=403, detail="Forbidden")
    mods = sb.table("modules").select("*").eq("course_id", course_id).order("id").execute()
    return mods.data or []


@router.patch(
    "/{course_id}/modules/{module_id}",
    response_model=ModuleOut,
    summary="Update module in course",
)
async def update_module(
    course_id: int,
    module_id: int,
    payload: ModuleUpdate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    crs = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not crs.data:
        raise HTTPException(status_code=404, detail="Course not found")
    if not _can_edit_course(user, crs.data[0]):
        raise HTTPException(status_code=403, detail="Forbidden")

    mod = sb.table("modules").select("*").eq("id", module_id).limit(1).execute()
    if not mod.data or mod.data[0].get("course_id") != course_id:
        raise HTTPException(status_code=404, detail="Module not found")

    data = payload.model_dump(exclude_unset=True)
    if not data:
        return mod.data[0]

    updated = sb.table("modules").update(data).eq("id", module_id).execute()
    if not updated.data:
        raise HTTPException(status_code=500, detail="Failed to update module")
    return updated.data[0]


@router.delete(
    "/{course_id}/modules/{module_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete module from course",
)
async def delete_module(
    course_id: int,
    module_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    crs = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    if not crs.data:
        raise HTTPException(status_code=404, detail="Course not found")
    if not _can_edit_course(user, crs.data[0]):
        raise HTTPException(status_code=403, detail="Forbidden")

    mod = sb.table("modules").select("*").eq("id", module_id).limit(1).execute()
    if not mod.data or mod.data[0].get("course_id") != course_id:
        raise HTTPException(status_code=404, detail="Module not found")

    asgs = sb.table("assignments").select("id").eq("module_id", module_id).execute()
    for row in asgs.data or []:
        delete_assignment_cascade(sb, row["id"])

    sb.table("module_materials").delete().eq("module_id", module_id).execute()
    sb.table("modules").delete().eq("id", module_id).execute()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
