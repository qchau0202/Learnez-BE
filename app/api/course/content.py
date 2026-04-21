"""Content/material CRUD on Supabase Storage + public.module_materials."""

from __future__ import annotations

import logging
import mimetypes
from typing import Any, List
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, require_roles
from app.models.course import MaterialOut
from app.services.notifications.scenario_notifications import notify_material_uploaded
from app.services.storage.cloudinary_service import (
    cloudinary_enabled,
    delete_public_id,
    public_id_from_url,
    signed_download_url,
    upload_bytes,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/content", tags=["Course & Content - Content"])

DEFAULT_MAX_MB = 10
ABSOLUTE_MAX_MB = 50


def _sb():
    supabase = get_supabase(service_role=True)
    if not supabase:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")
    return supabase


def _safe_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (name or "file.bin"))


def _stem_display_name(filename: str) -> str:
    """Human-readable default title from upload filename (strip path, drop last extension)."""
    base = (filename or "file").replace("\\", "/").split("/")[-1]
    if "." in base:
        return ".".join(base.split(".")[:-1]) or base
    return base


def _infer_material_type(mime: str | None) -> str:
    """UI category from MIME — not user-editable."""
    if not mime:
        return "file"
    m = mime.lower()
    if m.startswith("video/"):
        return "video"
    if m.startswith("audio/"):
        return "audio"
    if m.startswith("image/"):
        return "image"
    if m == "application/pdf":
        return "pdf"
    if m in ("application/msword",) or "wordprocessingml" in m or m.startswith("text/"):
        return "document"
    return "file"


def _require_cloudinary():
    if not cloudinary_enabled():
        raise HTTPException(
            status_code=500,
            detail=(
                "Cloudinary is not configured. Set CLOUDINARY_CLOUD_NAME, "
                "CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET."
            ),
        )


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


def _ensure_material_access(sb, user: dict[str, Any], course: dict) -> None:
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


@router.get("/modules/{module_id}/materials", response_model=List[MaterialOut])
async def list_module_materials(
    module_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """List materials of a module visible to the current role."""
    sb = _sb()
    _module, course = _get_module_and_course(sb, module_id)
    _ensure_material_access(sb, user, course)
    rows = sb.table("module_materials").select("*").eq("module_id", module_id).order("id", desc=True).execute()
    return rows.data or []


@router.get(
    "/materials/{material_id}/download",
    summary="Download or view a module material (auth-checked proxy)",
)
async def download_material(
    material_id: int,
    inline: bool = Query(False, description="If true, stream with Content-Disposition: inline."),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Stream a module material through the backend so access is always auth-checked.

    This avoids depending on Cloudinary public delivery (which is disabled by default
    for PDFs/ZIPs on new accounts) and hides the raw Cloudinary URL from the client.
    """
    sb = _sb()
    mat_res = sb.table("module_materials").select("*").eq("id", material_id).limit(1).execute()
    if not mat_res.data:
        raise HTTPException(status_code=404, detail="Material not found")
    row = mat_res.data[0]
    module_id = row.get("module_id")
    if module_id is None:
        raise HTTPException(status_code=404, detail="Material has no module")
    _module, course = _get_module_and_course(sb, module_id)
    _ensure_material_access(sb, user, course)

    file_url = row.get("file_url")
    public_id = row.get("cloudinary_public_id") or public_id_from_url(file_url)
    source_url: str | None = None
    if public_id:
        try:
            source_url = signed_download_url(public_id)
        except Exception as exc:  # noqa: BLE001 - fall back to direct URL
            logger.warning("signed_download_url failed for public_id=%s: %s", public_id, exc)
    if not source_url:
        source_url = file_url
    if not source_url:
        raise HTTPException(status_code=404, detail="Material has no file")

    # Preserve a sensible filename (name column + extension inferred from file_url).
    display_name = (row.get("name") or "download").strip() or "download"
    ext = ""
    try:
        tail = file_url.rsplit("?", 1)[0].rsplit("/", 1)[-1] if file_url else ""
        if "." in tail:
            ext = "." + tail.rsplit(".", 1)[-1]
    except Exception:
        ext = ""
    if ext and not display_name.lower().endswith(ext.lower()):
        display_name = f"{display_name}{ext}"

    mime = row.get("mime_type") or mimetypes.guess_type(display_name)[0] or "application/octet-stream"
    disposition = "inline" if inline else "attachment"
    # RFC 5987 encoded filename* for non-ASCII safety
    filename_star = quote(display_name, safe="")
    safe_ascii = display_name.encode("ascii", "ignore").decode("ascii") or "download"
    content_disposition = (
        f'{disposition}; filename="{safe_ascii}"; filename*=UTF-8\'\'{filename_star}'
    )

    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=120.0), follow_redirects=True)
    try:
        req = client.build_request("GET", source_url)
        upstream = await client.send(req, stream=True)
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        logger.warning("download_material upstream fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not fetch file from storage") from exc

    if upstream.status_code >= 400:
        status_code = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        logger.warning("download_material upstream returned %s for material %s", status_code, material_id)
        raise HTTPException(status_code=502, detail=f"Storage returned {status_code}")

    response_headers = {
        "Content-Disposition": content_disposition,
        "Cache-Control": "private, max-age=0, no-store",
    }
    content_length = upstream.headers.get("Content-Length")
    if content_length:
        response_headers["Content-Length"] = content_length

    async def iterator():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(iterator(), media_type=mime, headers=response_headers)

@router.post(
    "/modules/{module_id}/materials",
    response_model=MaterialOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload module material",
)
async def upload_content(
    module_id: int,
    file: UploadFile = File(..., description="Binary file to upload."),
    name: str | None = Form(None, description="Display name (defaults from filename)."),
    description: str | None = Form(None, description="Optional description."),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    """Upload material file to Cloudinary, then record metadata row (type derived from MIME)."""
    _require_cloudinary()
    sb = _sb()
    _module, course = _get_module_and_course(sb, module_id)
    if not _can_manage_module(user, course):
        raise HTTPException(status_code=403, detail="Forbidden")

    file_bytes = await file.read()
    _ensure_size_limit(None, file_bytes)
    filename = _safe_filename(file.filename or "file.bin")
    folder = f"learnez/courses/{course['id']}/modules/{module_id}"
    unique_name = f"{uuid4().hex}_{filename}"
    material_type = _infer_material_type(file.content_type)
    title = (name or "").strip() or _stem_display_name(file.filename or filename)
    desc = (description or "").strip() or None

    try:
        uploaded = upload_bytes(
            file_bytes,
            folder=folder,
            filename=unique_name,
            content_type=file.content_type,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage upload failed: {e}") from e

    public_url = uploaded.get("secure_url") or uploaded.get("url")
    if not public_url:
        raise HTTPException(status_code=500, detail="Cloudinary upload did not return public URL")
    ins = (
        sb.table("module_materials")
        .insert(
            {
                "module_id": module_id,
                "material_type": material_type,
                "file_url": public_url,
                "storage_provider": "cloudinary",
                "cloudinary_public_id": uploaded.get("public_id"),
                "mime_type": file.content_type,
                "size_bytes": int(uploaded.get("bytes") or len(file_bytes)),
                "metadata": {
                    "resource_type": uploaded.get("resource_type"),
                    "format": uploaded.get("format"),
                    "bytes": uploaded.get("bytes"),
                },
                "uploaded_by": user["user_id"],
                "name": title,
                "description": desc,
            }
        )
        .execute()
    )
    if not ins.data:
        try:
            public_id = uploaded.get("public_id")
            if public_id:
                delete_public_id(public_id)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Failed to create material record")
    mat = ins.data[0]
    notify_material_uploaded(
        sb,
        module_id=module_id,
        course_id=course["id"],
        material_id=mat["id"],
        material_label=title,
    )
    return mat


@router.put("/materials/{material_id}", response_model=MaterialOut, summary="Update module material")
async def update_material(
    material_id: int,
    file: UploadFile | None = File(None, description="New file to replace the current one (optional)."),
    name: str | None = Form(None, description="Display name (optional)."),
    description: str | None = Form(None, description="Description (optional)."),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    """Edit name/description and/or replace file (material_type follows MIME when file is replaced)."""
    _require_cloudinary()
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
    if name is not None:
        data["name"] = name.strip() or None
    if description is not None:
        data["description"] = (description or "").strip() or None

    old_public_id = row.get("cloudinary_public_id") or public_id_from_url(row.get("file_url"))
    new_path = None
    new_public_id = None
    if file is not None:
        file_bytes = await file.read()
        _ensure_size_limit(None, file_bytes)
        filename = _safe_filename(file.filename or "file.bin")
        folder = f"learnez/courses/{course['id']}/modules/{module_id}"
        unique_name = f"{uuid4().hex}_{filename}"
        try:
            uploaded = upload_bytes(
                file_bytes,
                folder=folder,
                filename=unique_name,
                content_type=file.content_type,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Storage upload failed: {e}") from e
        data["file_url"] = uploaded.get("secure_url") or uploaded.get("url")
        if not data["file_url"]:
            raise HTTPException(status_code=500, detail="Cloudinary upload did not return public URL")
        new_public_id = uploaded.get("public_id")
        new_path = data["file_url"]
        data["storage_provider"] = "cloudinary"
        data["cloudinary_public_id"] = new_public_id
        data["mime_type"] = file.content_type
        data["size_bytes"] = int(uploaded.get("bytes") or len(file_bytes))
        data["material_type"] = _infer_material_type(file.content_type)
        data["metadata"] = {
            "resource_type": uploaded.get("resource_type"),
            "format": uploaded.get("format"),
            "bytes": uploaded.get("bytes"),
        }
        data["uploaded_by"] = user["user_id"]
        if data.get("name") is None and file.filename:
            data["name"] = _stem_display_name(file.filename)

    if not data:
        return row

    upd = sb.table("module_materials").update(data).eq("id", material_id).execute()
    if not upd.data:
        if new_public_id:
            try:
                delete_public_id(new_public_id)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail="Failed to update material")

    if new_path and old_public_id:
        try:
            delete_public_id(old_public_id)
        except Exception:
            pass

    return upd.data[0]


@router.delete("/materials/{material_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete module material")
async def delete_material(
    material_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    _require_cloudinary()
    sb = _sb()
    mat = sb.table("module_materials").select("*").eq("id", material_id).limit(1).execute()
    if not mat.data:
        raise HTTPException(status_code=404, detail="Material not found")
    row = mat.data[0]
    module_id = row["module_id"]
    _module, course = _get_module_and_course(sb, module_id)
    if not _can_manage_module(user, course):
        raise HTTPException(status_code=403, detail="Forbidden")

    old_public_id = row.get("cloudinary_public_id") or public_id_from_url(row.get("file_url"))
    sb.table("module_materials").delete().eq("id", material_id).execute()
    if old_public_id:
        try:
            delete_public_id(old_public_id)
        except Exception:
            pass
    return Response(status_code=status.HTTP_204_NO_CONTENT)
