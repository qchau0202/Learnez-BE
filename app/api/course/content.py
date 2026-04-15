"""Content/material CRUD on Supabase Storage + public.module_materials."""

from __future__ import annotations

from typing import Any, List
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status

from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, require_roles
from app.models.course import MaterialOut
from app.services.notifications.scenario_notifications import notify_material_uploaded

router = APIRouter(prefix="/content", tags=["Course & Content - Content"])

BUCKET_NAME = "module-materials"
DEFAULT_MAX_MB = 10
ABSOLUTE_MAX_MB = 50


def _sb():
    supabase = get_supabase(service_role=True)
    if not supabase:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")
    return supabase


def _safe_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (name or "file.bin"))


def _extract_storage_path(file_url: str | None) -> str | None:
    if not file_url:
        return None
    marker = f"/{BUCKET_NAME}/"
    if marker in file_url:
        return file_url.split(marker, 1)[1]
    return None


def _public_url_for(sb, path: str) -> str:
    raw = sb.storage.from_(BUCKET_NAME).get_public_url(path)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return raw.get("publicURL") or raw.get("public_url") or ""
    return str(raw)


def _get_module_and_course(sb, module_id: int) -> tuple[dict, dict]:
    mod = sb.table("modules").select("*").eq("id", module_id).limit(1).execute()
    if not mod.data:
        raise HTTPException(status_code=404, detail="Module not found")
    mrow = mod.data[0]
    crs = sb.table("courses").select("*").eq("id", mrow["course_id"]).limit(1).execute()
    if not crs.data:
        raise HTTPException(status_code=404, detail="Course not found")
    return mrow, crs.data[0]


def _can_manage_module(user: dict[str, Any], course_row: dict) -> bool:
    role = ROLE_MAP.get(user.get("role_id"))
    if role == "Admin":
        return True
    return role == "Lecturer" and course_row.get("lecturer_id") == user.get("user_id")


def _ensure_size_limit(max_size_mb: int | None, file_bytes: bytes):
    limit_mb = DEFAULT_MAX_MB if max_size_mb is None else max_size_mb
    if limit_mb < 1 or limit_mb > ABSOLUTE_MAX_MB:
        raise HTTPException(
            status_code=400,
            detail=f"max_size_mb must be between 1 and {ABSOLUTE_MAX_MB}",
        )
    max_bytes = limit_mb * 1024 * 1024
    if len(file_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(file_bytes)} bytes). Limit is {max_bytes} bytes ({limit_mb}MB).",
        )


@router.get("/modules/{module_id}/materials", response_model=List[MaterialOut])
async def list_module_materials(
    module_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """List materials of a module visible to the current role."""
    sb = _sb()
    _module, course = _get_module_and_course(sb, module_id)
    role = ROLE_MAP.get(user.get("role_id"))
    uid = user.get("user_id")
    if role == "Student":
        enr = (
            sb.table("course_enrollments")
            .select("course_id")
            .eq("course_id", course["id"])
            .eq("student_id", uid)
            .limit(1)
            .execute()
        )
        if not enr.data:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif role == "Lecturer" and not _can_manage_module(user, course):
        raise HTTPException(status_code=403, detail="Forbidden")
    rows = sb.table("module_materials").select("*").eq("module_id", module_id).order("id", desc=True).execute()
    return rows.data or []

@router.post(
    "/modules/{module_id}/materials",
    response_model=MaterialOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload module material",
)
async def upload_content(
    module_id: int,
    file: UploadFile = File(..., description="Binary file to upload."),
    material_type: str = Form("file", description="Label/type for material (e.g. file, video, link)."),
    max_size_mb: int = Form(DEFAULT_MAX_MB, description=f"Per-request max file size in MB (1-{ABSOLUTE_MAX_MB})."),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    """Upload material file to bucket, then record metadata row."""
    sb = _sb()
    _module, course = _get_module_and_course(sb, module_id)
    if not _can_manage_module(user, course):
        raise HTTPException(status_code=403, detail="Forbidden")

    file_bytes = await file.read()
    _ensure_size_limit(max_size_mb, file_bytes)
    filename = _safe_filename(file.filename or "file.bin")
    path = f"course_{course['id']}/module_{module_id}/{uuid4().hex}_{filename}"

    try:
        sb.storage.from_(BUCKET_NAME).upload(
            path,
            file_bytes,
            {"content-type": file.content_type or "application/octet-stream"},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage upload failed: {e}") from e

    public_url = _public_url_for(sb, path)
    ins = (
        sb.table("module_materials")
        .insert(
            {
                "module_id": module_id,
                "material_type": material_type,
                "file_url": public_url,
                "uploaded_by": user["user_id"],
            }
        )
        .execute()
    )
    if not ins.data:
        try:
            sb.storage.from_(BUCKET_NAME).remove([path])
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Failed to create material record")
    mat = ins.data[0]
    notify_material_uploaded(
        sb,
        module_id=module_id,
        course_id=course["id"],
        material_id=mat["id"],
        material_label=filename,
    )
    return mat


@router.put("/materials/{material_id}", response_model=MaterialOut, summary="Update module material")
async def update_material(
    material_id: int,
    file: UploadFile | None = File(None, description="New file to replace the current one (optional)."),
    material_type: str | None = Form(None, description="New material type (optional)."),
    max_size_mb: int = Form(DEFAULT_MAX_MB, description=f"Per-request max file size in MB (1-{ABSOLUTE_MAX_MB})."),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    """Edit material type and/or replace file object."""
    sb = _sb()
    mat = sb.table("module_materials").select("*").eq("id", material_id).limit(1).execute()
    if not mat.data:
        raise HTTPException(status_code=404, detail="Material not found")
    row = mat.data[0]
    module_id = row["module_id"]
    _module, course = _get_module_and_course(sb, module_id)
    if not _can_manage_module(user, course):
        raise HTTPException(status_code=403, detail="Forbidden")

    data: dict[str, Any] = {}
    if material_type is not None:
        data["material_type"] = material_type

    old_path = _extract_storage_path(row.get("file_url"))
    new_path = None
    if file is not None:
        file_bytes = await file.read()
        _ensure_size_limit(max_size_mb, file_bytes)
        filename = _safe_filename(file.filename or "file.bin")
        new_path = f"course_{course['id']}/module_{module_id}/{uuid4().hex}_{filename}"
        try:
            sb.storage.from_(BUCKET_NAME).upload(
                new_path,
                file_bytes,
                {"content-type": file.content_type or "application/octet-stream"},
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Storage upload failed: {e}") from e
        data["file_url"] = _public_url_for(sb, new_path)
        data["uploaded_by"] = user["user_id"]

    if not data:
        return row

    upd = sb.table("module_materials").update(data).eq("id", material_id).execute()
    if not upd.data:
        if new_path:
            try:
                sb.storage.from_(BUCKET_NAME).remove([new_path])
            except Exception:
                pass
        raise HTTPException(status_code=500, detail="Failed to update material")

    if new_path and old_path:
        try:
            sb.storage.from_(BUCKET_NAME).remove([old_path])
        except Exception:
            pass

    return upd.data[0]


@router.delete("/materials/{material_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete module material")
async def delete_material(
    material_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    mat = sb.table("module_materials").select("*").eq("id", material_id).limit(1).execute()
    if not mat.data:
        raise HTTPException(status_code=404, detail="Material not found")
    row = mat.data[0]
    module_id = row["module_id"]
    _module, course = _get_module_and_course(sb, module_id)
    if not _can_manage_module(user, course):
        raise HTTPException(status_code=403, detail="Forbidden")

    old_path = _extract_storage_path(row.get("file_url"))
    sb.table("module_materials").delete().eq("id", material_id).execute()
    if old_path:
        try:
            sb.storage.from_(BUCKET_NAME).remove([old_path])
        except Exception:
            pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)
