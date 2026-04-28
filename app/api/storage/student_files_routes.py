"""Student file management API routes (CRUD) - Supabase PostgreSQL backed."""

from __future__ import annotations

import mimetypes
import os
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import StreamingResponse

from app.api.deps import DbDep
from app.core.dependencies import ROLE_MAP, require_roles
from app.models.student_files import (
    BulkDeleteRequest,
    FileListResponse,
    FolderListResponse,
    StorageUsageResponse,
    StudentFileDetailResponse,
    StudentFileResponse,
    StudentFileUpdate,
    StudentFolderCreate,
    StudentFolderDetailResponse,
    StudentFolderResponse,
    StudentFolderUpdate,
)
from app.services.storage.cloudinary_service import delete_public_id, public_id_from_url, upload_bytes
from app.services.storage.cloudinary_service import signed_download_url
from app.services.storage.student_files_db import StudentFileService

router = APIRouter(prefix="/storage/student-files", tags=["Student File Management"])

# Configuration
ALLOWED_EXTENSIONS = {
    # docs
    "pdf",
    "txt",
    "rtf",
    "doc",
    "docx",
    "odt",
    "ppt",
    "pptx",
    "odp",
    "xls",
    "xlsx",
    "ods",
    "csv",
    # images
    "jpg",
    "jpeg",
    "png",
    "gif",
    "webp",
    "bmp",
    "heic",
    "svg",
    # archives
    "zip",
    "rar",
    "7z",
}
FORBIDDEN_CONTENT_PREFIX = ("video/", "audio/")
DEFAULT_MAX_FILE_MB = 5  # 5MB per file
DEFAULT_STUDENT_QUOTA_MB = 10  # 10MB total per student as specified


def _safe_filename(name: str) -> str:
    """Sanitize filename."""
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)


def _extension(name: str) -> str:
    """Extract file extension."""
    from pathlib import Path

    return Path(name).suffix.lower().lstrip(".")


def _enforce_file_type(file: UploadFile) -> None:
    """Validate file type and content."""
    ext = _extension(file.filename or "")
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '.{ext or '?'}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )
    if (file.content_type or "").lower().startswith(FORBIDDEN_CONTENT_PREFIX):
        raise HTTPException(
            status_code=400, detail="Video/audio uploads are not allowed for student files"
        )


def _max_file_bytes() -> int:
    """Get max file size in bytes."""
    mb = int(os.getenv("STUDENT_FILE_MAX_MB", str(DEFAULT_MAX_FILE_MB)))
    return mb * 1024 * 1024


def _ensure_cloudinary_configured() -> None:
    """Verify Cloudinary is configured."""
    try:
        from app.services.storage.cloudinary_service import ensure_cloudinary_configured

        ensure_cloudinary_configured()
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Cloudinary not configured: {str(e)}"
        ) from e


def _guess_resource_type(file_data: dict[str, Any]) -> str:
    file_url = (file_data.get("file_url") or "").strip()
    from urllib.parse import urlparse

    path = urlparse(file_url).path if file_url else ""
    for resource_type in ("image", "raw", "video", "auto"):
        if f"/{resource_type}/upload/" in path:
            return resource_type

    mime_type = (file_data.get("mime_type") or "").lower()
    if mime_type.startswith("image/"):
        return "image"
    return "raw"


def _file_resource_type_candidates(file_data: dict[str, Any]) -> list[str]:
    candidates: list[str] = []

    meta = file_data.get("metadata")
    if isinstance(meta, dict):
        rt = meta.get("resource_type")
        if isinstance(rt, str) and rt.strip():
            candidates.append(rt.strip().lower())

    guessed = _guess_resource_type(file_data)
    if guessed:
        candidates.append(guessed)

    candidates.extend(["raw", "image", "auto", "video"])

    seen: set[str] = set()
    deduped: list[str] = []
    for item in candidates:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped or ["raw"]


# ============================================================================
# FOLDER ENDPOINTS
# ============================================================================


@router.post("/folders", response_model=StudentFolderResponse, status_code=status.HTTP_201_CREATED)
async def create_folder(
    folder_data: StudentFolderCreate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Create a new folder for file organization."""
    try:
        folder = await StudentFileService.create_folder(
            student_id=UUID(user["user_id"]),
            folder_name=folder_data.folder_name,
            parent_folder_id=folder_data.parent_folder_id,
            description=folder_data.description,
        )
        return folder
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/folders", response_model=FolderListResponse)
async def list_folders(
    parent_folder_id: int | None = None,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """List all folders for the current user."""
    try:
        folders = await StudentFileService.list_folders(
            student_id=UUID(user["user_id"]), parent_folder_id=parent_folder_id
        )
        return FolderListResponse(items=folders, total=len(folders))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/folders/{folder_id}", response_model=StudentFolderDetailResponse)
async def get_folder(
    folder_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Get folder details."""
    try:
        folder = await StudentFileService.get_folder(
            folder_id=folder_id, student_id=UUID(user["user_id"])
        )

        # Get file count in folder
        from app.core.database import get_supabase

        supabase = get_supabase()
        files = (
            supabase.table("student_files")
            .select("id")
            .eq("folder_id", folder_id)
            .eq("is_deleted", False)
            .execute()
        )

        folder["file_count"] = len(files.data or [])
        return folder
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/folders/{folder_id}", response_model=StudentFolderResponse)
async def update_folder(
    folder_id: int,
    folder_data: StudentFolderUpdate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Update folder metadata."""
    try:
        folder = await StudentFileService.update_folder(
            folder_id=folder_id,
            student_id=UUID(user["user_id"]),
            folder_name=folder_data.folder_name,
            description=folder_data.description,
            parent_folder_id=folder_data.parent_folder_id,
        )
        return folder
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    folder_id: int,
    force: bool = False,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Delete folder (must be empty unless force=true)."""
    try:
        await StudentFileService.delete_folder(
            folder_id=folder_id, student_id=UUID(user["user_id"]), force=force
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# FILE ENDPOINTS
# ============================================================================


@router.post("/files/upload", response_model=StudentFileResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    file_title: str = Form(..., min_length=1, max_length=500),
    folder_id: int | None = Form(None),
    description: str | None = Form(None),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Upload a file to Cloudinary and create metadata record."""
    _ensure_cloudinary_configured()
    _enforce_file_type(file)

    try:
        file_bytes = await file.read()
        max_bytes = _max_file_bytes()

        if len(file_bytes) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum is {max_bytes // (1024 * 1024)}MB",
            )

        student_id = UUID(user["user_id"])

        # Check storage quota
        usage = await StudentFileService.get_storage_usage(student_id)
        if usage["remaining_bytes"] < len(file_bytes):
            raise HTTPException(
                status_code=413,
                detail=f"Storage quota exceeded. Remaining: {usage['remaining_bytes']} bytes, "
                f"file size: {len(file_bytes)} bytes",
            )

        # Upload to Cloudinary
        safe_filename = _safe_filename(file.filename or "file")
        from uuid import uuid4

        cloud_folder = f"learnez/students/{student_id}/files"
        if folder_id:
            folder = await StudentFileService.get_folder(folder_id, student_id)
            cloud_folder += f"/{folder['folder_name']}"

        try:
            uploaded = upload_bytes(
                file_bytes,
                folder=cloud_folder,
                filename=f"{uuid4().hex}_{safe_filename}",
                content_type=file.content_type,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Cloudinary upload failed: {str(e)}")

        file_url = uploaded.get("secure_url") or uploaded.get("url")
        public_id = uploaded.get("public_id")

        if not file_url or not public_id:
            raise HTTPException(
                status_code=500, detail="Cloudinary upload result missing secure_url or public_id"
            )

        # Create database record
        file_record = await StudentFileService.create_file(
            student_id=student_id,
            file_name=safe_filename,
            file_title=file_title,
            file_url=file_url,
            mime_type=file.content_type,
            size_bytes=int(uploaded.get("bytes") or len(file_bytes)),
            storage_provider="cloudinary",
            cloudinary_public_id=public_id,
            folder_id=folder_id,
            description=description,
            uploaded_by=student_id,
            metadata={
                "resource_type": uploaded.get("resource_type"),
                "format": uploaded.get("format"),
                "bytes": uploaded.get("bytes"),
            },
        )

        return file_record

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/files", response_model=FileListResponse)
async def list_files(
    folder_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """List files for the current user."""
    try:
        if limit > 500:
            limit = 500
        if offset < 0:
            offset = 0

        files, total = await StudentFileService.list_files(
            student_id=UUID(user["user_id"]), folder_id=folder_id, limit=limit, offset=offset
        )

        usage = await StudentFileService.get_storage_usage(UUID(user["user_id"]))
        storage_used_mb = usage["used_bytes"] / (1024 * 1024)

        return FileListResponse(
            items=files, total=total, folder_id=folder_id, storage_used_mb=round(storage_used_mb, 2)
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/files/{file_id}", response_model=StudentFileDetailResponse)
async def get_file(
    file_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Get file details."""
    try:
        file_data = await StudentFileService.get_file(
            file_id=file_id, student_id=UUID(user["user_id"])
        )

        # Get folder path
        folder_path = await StudentFileService.get_folder_path(
            file_data.get("folder_id"), UUID(user["user_id"])
        )

        file_data["folder_location"] = folder_path
        return file_data

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/files/{file_id}", response_model=StudentFileResponse)
async def update_file(
    file_id: int,
    file_data: StudentFileUpdate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Update file metadata (title, description, folder)."""
    try:
        updated_file = await StudentFileService.update_file(
            file_id=file_id,
            student_id=UUID(user["user_id"]),
            file_title=file_data.file_title,
            description=file_data.description,
            folder_id=file_data.folder_id,
        )
        return updated_file

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/files/{file_id}/download", summary="Download a student file")
async def download_file(
    file_id: int,
    inline: bool = False,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Stream a student file through the backend as an authenticated download."""
    try:
        file_data = await StudentFileService.get_file(file_id=file_id, student_id=UUID(user["user_id"]))

        file_url = file_data.get("file_url")
        public_id = file_data.get("cloudinary_public_id") or public_id_from_url(file_url)
        source_url: str | None = None
        resource_type_candidates = _file_resource_type_candidates(file_data)

        if public_id:
            for rt in resource_type_candidates:
                try:
                    source_url = signed_download_url(public_id, resource_type=rt)
                    if source_url:
                        break
                except Exception:
                    continue

        if not source_url:
            source_url = file_url

        if not source_url:
            raise HTTPException(status_code=404, detail="File has no downloadable URL")

        display_name = (file_data.get("file_name") or file_data.get("file_title") or "download").strip() or "download"
        mime_type = file_data.get("mime_type") or mimetypes.guess_type(display_name)[0] or "application/octet-stream"
        safe_ascii = display_name.encode("ascii", "ignore").decode("ascii") or "download"
        filename_star = quote(display_name, safe="")
        disposition = "inline" if inline else "attachment"
        content_disposition = f'{disposition}; filename="{safe_ascii}"; filename*=UTF-8\'\'{filename_star}'

        client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=120.0), follow_redirects=True)
        try:
            request = client.build_request("GET", source_url)
            upstream = await client.send(request, stream=True)
        except Exception as exc:
            await client.aclose()
            raise HTTPException(status_code=502, detail="Could not fetch file from storage") from exc

        if upstream.status_code >= 400:
            status_code = upstream.status_code
            await upstream.aclose()

            if public_id and source_url != file_url:
                recovered = False
                for rt in resource_type_candidates[1:]:
                    try:
                        alt_url = signed_download_url(public_id, resource_type=rt)
                        req_alt = client.build_request("GET", alt_url)
                        upstream = await client.send(req_alt, stream=True)
                    except Exception:
                        continue
                    if upstream.status_code < 400:
                        recovered = True
                        break
                    await upstream.aclose()

                if not recovered:
                    if file_url:
                        try:
                            req2 = client.build_request("GET", file_url)
                            upstream = await client.send(req2, stream=True)
                        except Exception as exc:
                            await client.aclose()
                            raise HTTPException(status_code=502, detail="Could not fetch file from storage") from exc
                        if upstream.status_code >= 400:
                            bad_status = upstream.status_code
                            await upstream.aclose()
                            await client.aclose()
                            raise HTTPException(status_code=502, detail=f"Storage returned {bad_status}")
                    else:
                        await client.aclose()
                        raise HTTPException(status_code=502, detail=f"Storage returned {status_code}")
            else:
                await client.aclose()
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

        return StreamingResponse(iterator(), media_type=mime_type, headers=response_headers)

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    file_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Delete a file (soft delete)."""
    try:
        await StudentFileService.delete_file(file_id=file_id, student_id=UUID(user["user_id"]))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# STORAGE QUOTA ENDPOINTS
# ============================================================================


@router.get("/usage", response_model=StorageUsageResponse)
async def get_storage_usage(
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Get storage quota and usage information."""
    try:
        usage = await StudentFileService.get_storage_usage(UUID(user["user_id"]))
        usage["used_mb"] = round(usage["used_bytes"] / (1024 * 1024), 2)
        usage["quota_mb"] = round(usage["quota_bytes"] / (1024 * 1024), 2)
        return usage

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# BULK OPERATIONS
# ============================================================================


@router.post("/bulk-delete", status_code=status.HTTP_204_NO_CONTENT)
async def bulk_delete(
    request: BulkDeleteRequest,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    """Delete multiple files or folders at once."""
    try:
        student_id = UUID(user["user_id"])

        if request.resource_type == "file":
            for file_id in request.ids:
                try:
                    await StudentFileService.delete_file(file_id, student_id)
                except Exception:
                    pass  # Continue with other files

        elif request.resource_type == "folder":
            for folder_id in request.ids:
                try:
                    await StudentFileService.delete_folder(folder_id, student_id, force=False)
                except Exception:
                    pass  # Continue with other folders

        return Response(status_code=status.HTTP_204_NO_CONTENT)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
