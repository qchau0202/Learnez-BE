"""Course CRUD — aligned with public.courses (Supabase)."""

from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, require_roles
from app.models.course import CourseCreate, CourseOut, CourseUpdate, ModuleCreate, ModuleOut
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


@router.post("/", response_model=CourseOut, status_code=status.HTTP_201_CREATED)
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
        "lecturer_id": payload.lecturer_id,
        "schedule": payload.schedule.isoformat() if payload.schedule else None,
        "is_complete": payload.is_complete,
        "created_by": user["user_id"],
    }
    row = {k: v for k, v in row.items() if v is not None}
    created = sb.table("courses").insert(row).execute()
    if not created.data:
        raise HTTPException(status_code=500, detail="Failed to create course")
    return created.data[0]


@router.get("/", response_model=List[CourseOut])
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


@router.get("/{course_id}", response_model=CourseOut)
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
    return row


@router.put("/{course_id}", response_model=CourseOut)
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
    if not data:
        return row

    updated = sb.table("courses").update(data).eq("id", course_id).execute()
    if not updated.data:
        raise HTTPException(status_code=500, detail="Failed to update course")
    return updated.data[0]


@router.delete("/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
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


@router.get("/{course_id}/modules", response_model=List[ModuleOut])
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


@router.delete(
    "/{course_id}/modules/{module_id}",
    status_code=status.HTTP_204_NO_CONTENT,
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
