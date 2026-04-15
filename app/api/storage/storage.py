"""Student file management on Cloudinary + Mongo metadata (files + folders)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status

from app.api.deps import DbDep
from app.core.dependencies import ROLE_MAP, require_roles
from app.services.storage.cloudinary_service import (
    cloudinary_enabled,
    delete_public_id,
    public_id_from_url,
    upload_bytes,
)

router = APIRouter(prefix="/storage", tags=["File Management"])

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
DEFAULT_MAX_FILE_MB = 25
DEFAULT_STUDENT_QUOTA_MB = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)


def _require_cloudinary() -> None:
    if not cloudinary_enabled():
        raise HTTPException(
            status_code=500,
            detail=(
                "Cloudinary is not configured. Set CLOUDINARY_CLOUD_NAME, "
                "CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET."
            ),
        )


def _extension(name: str) -> str:
    return Path(name).suffix.lower().lstrip(".")


def _enforce_file_type(file: UploadFile) -> None:
    ext = _extension(file.filename or "")
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '.{ext or '?'}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )
    if (file.content_type or "").lower().startswith(FORBIDDEN_CONTENT_PREFIX):
        raise HTTPException(status_code=400, detail="Video/audio uploads are not allowed for student files")


def _max_file_bytes() -> int:
    mb = int(os.getenv("STUDENT_FILE_MAX_MB", str(DEFAULT_MAX_FILE_MB)))
    return mb * 1024 * 1024


def _student_quota_bytes() -> int:
    mb = int(os.getenv("STUDENT_STORAGE_QUOTA_MB", str(DEFAULT_STUDENT_QUOTA_MB)))
    return mb * 1024 * 1024


def _oid(value: str, *, name: str) -> ObjectId:
    try:
        return ObjectId(value)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid {name}") from e


async def _usage_for_user(db: DbDep, user_id: str) -> tuple[int, int]:
    coll = db["student_files"]
    pipeline = [
        {"$match": {"owner_id": user_id, "is_deleted": {"$ne": True}}},
        {"$group": {"_id": None, "total_bytes": {"$sum": {"$ifNull": ["$size_bytes", 0]}}}},
    ]
    rows = await coll.aggregate(pipeline).to_list(length=1)
    used = int(rows[0]["total_bytes"]) if rows else 0
    quota = _student_quota_bytes()
    return used, quota


def _file_doc_to_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["_id"]),
        "owner_id": row["owner_id"],
        "folder_id": str(row["folder_id"]) if row.get("folder_id") else None,
        "name": row.get("name"),
        "size_bytes": int(row.get("size_bytes") or 0),
        "mime_type": row.get("mime_type"),
        "file_url": row.get("file_url"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _folder_doc_to_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["_id"]),
        "owner_id": row["owner_id"],
        "name": row["name"],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


@router.post("/folders", summary="Create folder for my files", status_code=status.HTTP_201_CREATED)
async def create_folder(
    db: DbDep,
    name: str = Form(..., min_length=1, max_length=120),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    owner_id = user["user_id"]
    now = _now_iso()
    folder_name = _safe_filename(name.strip())
    if not folder_name:
        raise HTTPException(status_code=400, detail="Folder name cannot be empty")
    exists = await db["student_folders"].find_one({"owner_id": owner_id, "name": folder_name, "is_deleted": {"$ne": True}})
    if exists:
        raise HTTPException(status_code=409, detail="Folder already exists")
    doc = {
        "owner_id": owner_id,
        "name": folder_name,
        "is_deleted": False,
        "created_at": now,
        "updated_at": now,
    }
    ins = await db["student_folders"].insert_one(doc)
    saved = await db["student_folders"].find_one({"_id": ins.inserted_id})
    return _folder_doc_to_out(saved)


@router.get("/folders", summary="List my folders")
async def list_folders(
    db: DbDep,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    owner_id = user["user_id"]
    rows = (
        await db["student_folders"]
        .find({"owner_id": owner_id, "is_deleted": {"$ne": True}})
        .sort("name", 1)
        .to_list(length=500)
    )
    return [_folder_doc_to_out(r) for r in rows]


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete folder (must be empty)")
async def delete_folder(
    folder_id: str,
    db: DbDep,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    owner_id = user["user_id"]
    oid = _oid(folder_id, name="folder_id")
    folder = await db["student_folders"].find_one({"_id": oid, "is_deleted": {"$ne": True}})
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    if folder["owner_id"] != owner_id and ROLE_MAP.get(user.get("role_id")) != "Admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    used = await db["student_files"].count_documents({"folder_id": oid, "is_deleted": {"$ne": True}})
    if used > 0:
        raise HTTPException(status_code=409, detail="Folder is not empty. Delete or move files first.")
    await db["student_folders"].update_one({"_id": oid}, {"$set": {"is_deleted": True, "updated_at": _now_iso()}})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/upload",
    summary="Upload student file (Cloudinary)",
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    db: DbDep,
    file: UploadFile = File(...),
    folder_id: str | None = Form(None, description="Optional folder id created via /storage/folders"),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    _require_cloudinary()
    _enforce_file_type(file)
    file_bytes = await file.read()
    max_b = _max_file_bytes()
    if len(file_bytes) > max_b:
        raise HTTPException(status_code=413, detail=f"File too large. Limit is {max_b} bytes")

    owner_id = user["user_id"]
    folder_oid: ObjectId | None = None
    folder_name = "root"
    if folder_id:
        folder_oid = _oid(folder_id, name="folder_id")
        folder = await db["student_folders"].find_one({"_id": folder_oid, "is_deleted": {"$ne": True}})
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found")
        if folder["owner_id"] != owner_id and ROLE_MAP.get(user.get("role_id")) != "Admin":
            raise HTTPException(status_code=403, detail="Forbidden for target folder")
        folder_name = folder["name"]

    role = ROLE_MAP.get(user.get("role_id"))
    if role == "Student":
        used, quota = await _usage_for_user(db, owner_id)
        if used + len(file_bytes) > quota:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Storage quota exceeded. Used={used} bytes, incoming={len(file_bytes)} bytes, "
                    f"quota={quota} bytes"
                ),
            )

    safe_name = _safe_filename(file.filename or f"file_{uuid4().hex}.bin")
    folder = f"learnez/students/{owner_id}/files/{folder_name}"
    try:
        uploaded = upload_bytes(
            file_bytes,
            folder=folder,
            filename=f"{uuid4().hex}_{safe_name}",
            content_type=file.content_type,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cloudinary upload failed: {e}") from e

    file_url = uploaded.get("secure_url") or uploaded.get("url")
    public_id = uploaded.get("public_id")
    if not file_url or not public_id:
        raise HTTPException(status_code=500, detail="Cloudinary upload result missing secure_url/public_id")

    now = _now_iso()
    doc = {
        "owner_id": owner_id,
        "folder_id": folder_oid,
        "name": safe_name,
        "mime_type": file.content_type,
        "size_bytes": int(uploaded.get("bytes") or len(file_bytes)),
        "file_url": file_url,
        "cloudinary_public_id": public_id,
        "is_deleted": False,
        "created_at": now,
        "updated_at": now,
    }
    res = await db["student_files"].insert_one(doc)
    saved = await db["student_files"].find_one({"_id": res.inserted_id})
    return _file_doc_to_out(saved)


@router.get("/files", summary="List my files")
async def list_files(
    db: DbDep,
    folder_id: str | None = None,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    owner_id = user["user_id"]
    query: dict[str, Any] = {"owner_id": owner_id, "is_deleted": {"$ne": True}}
    if folder_id:
        query["folder_id"] = _oid(folder_id, name="folder_id")
    rows = await db["student_files"].find(query).sort("created_at", -1).to_list(length=500)
    return [_file_doc_to_out(r) for r in rows]


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete my file")
async def delete_file(
    file_id: str,
    db: DbDep,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    oid = _oid(file_id, name="file_id")
    row = await db["student_files"].find_one({"_id": oid, "is_deleted": {"$ne": True}})
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    if row.get("owner_id") != user["user_id"] and ROLE_MAP.get(user.get("role_id")) != "Admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    public_id = row.get("cloudinary_public_id") or public_id_from_url(row.get("file_url"))
    if public_id:
        try:
            delete_public_id(public_id)
        except Exception:
            pass
    await db["student_files"].update_one({"_id": oid}, {"$set": {"is_deleted": True, "updated_at": _now_iso()}})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/usage", summary="Get my storage usage and quota")
async def get_storage_usage(
    db: DbDep,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    used, quota = await _usage_for_user(db, user["user_id"])
    return {
        "owner_id": user["user_id"],
        "used_bytes": used,
        "quota_bytes": quota,
        "remaining_bytes": max(quota - used, 0),
        "used_mb": round(used / (1024 * 1024), 2),
        "quota_mb": round(quota / (1024 * 1024), 2),
    }
