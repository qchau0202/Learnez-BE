"""Course CRUD — aligned with public.courses (Supabase)."""

import logging
from datetime import timedelta
from typing import Any, List

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, require_roles
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
    user: dict[str, Any] = Depends(require_roles(["Admin"])),
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
        "course_start_date": payload.course_start_date.isoformat() if payload.course_start_date else None,
        "course_end_date": payload.course_end_date.isoformat() if payload.course_end_date else None,
        "lecturer_id": payload.lecturer_id,
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


@router.get("/", response_model=List[CourseOut], summary="List courses by role visibility")
async def list_courses(user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"]))):
    sb = _sb()
    role = ROLE_MAP.get(user["role_id"])
    uid = user["user_id"]

    if role == "Admin":
        res = sb.table("courses").select("*").order("id", desc=True).execute()
        return res.data or []

    if role == "Lecturer":
        res = (
            sb.table("courses")
            .select("*")
            .eq("lecturer_id", uid)
            .order("id", desc=True)
            .execute()
        )
        return res.data or []

    enr = sb.table("course_enrollments").select("course_id").eq("student_id", uid).execute()
    ids = [r["course_id"] for r in (enr.data or [])]
    if not ids:
        return []
    res = sb.table("courses").select("*").in_("id", ids).order("id", desc=True).execute()
    return res.data or []


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
        lmeta = lecturer_meta_by_id.get(c.get("lecturer_id"), {})
        row = {
            **c,
            "student_count": count_map.get(c["id"], 0),
            "faculty_id": lmeta.get("faculty_id"),
            "faculty_name": lmeta.get("faculty_name"),
            "department_id": lmeta.get("department_id"),
            "department_name": lmeta.get("department_name"),
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
    return out


@router.put("/{course_id}", response_model=CourseOut, summary="Update course")
async def update_course(
    course_id: int,
    payload: CourseUpdate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
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
    user: dict[str, Any] = Depends(require_roles(["Admin"])),
):
    sb = _sb()
    res = sb.table("courses").select("id").eq("id", course_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Course not found")
    sb.table("courses").delete().eq("id", course_id).execute()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
