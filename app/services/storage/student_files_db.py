"""Database service layer for student file management (Supabase PostgreSQL)."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from app.core.database import get_supabase


class StudentFileService:
    """Service for managing student folders and files in Supabase."""

    STUDENT_QUOTA_MB = 10  # 10MB per student as specified
    STUDENT_QUOTA_BYTES = STUDENT_QUOTA_MB * 1024 * 1024

    @staticmethod
    def _cloudinary_resource_type_candidates(file_data: dict[str, Any]) -> list[str]:
        candidates: list[str] = []

        metadata = file_data.get("metadata")
        if isinstance(metadata, dict):
            rt = metadata.get("resource_type")
            if isinstance(rt, str) and rt.strip():
                candidates.append(rt.strip().lower())

        file_url = (file_data.get("file_url") or "").strip()
        if "/image/upload/" in file_url:
            candidates.append("image")
        elif "/video/upload/" in file_url:
            candidates.append("video")
        elif "/raw/upload/" in file_url:
            candidates.append("raw")

        mime = (file_data.get("mime_type") or "").lower()
        if mime.startswith("image/"):
            candidates.append("image")
        elif mime.startswith("video/"):
            candidates.append("video")
        else:
            candidates.append("raw")

        candidates.extend(["raw", "image", "video", "auto"])

        deduped: list[str] = []
        seen: set[str] = set()
        for rt in candidates:
            key = rt.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        return deduped or ["raw"]

    @staticmethod
    def _supabase():
        """Get Supabase client."""
        client = get_supabase(service_role=True)
        if not client:
            raise RuntimeError("Missing SUPABASE_SERVICE_ROLE_KEY")
        return client

    # ========================================================================
    # FOLDER OPERATIONS
    # ========================================================================

    @staticmethod
    async def create_folder(
        student_id: UUID,
        folder_name: str,
        parent_folder_id: Optional[int] = None,
        description: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a new folder for student."""
        supabase = StudentFileService._supabase()

        # Validate parent folder exists if provided
        if parent_folder_id:
            parent = (
                supabase.table("student_folders")
                .select("id, student_id")
                .eq("id", parent_folder_id)
                .eq("is_deleted", False)
                .single()
                .execute()
            )
            if not parent.data or parent.data.get("student_id") != str(student_id):
                raise ValueError(f"Parent folder {parent_folder_id} not found or access denied")

        # Check duplicate folder name at same level
        query = (
            supabase.table("student_folders")
            .select("id")
            .eq("student_id", str(student_id))
            .eq("folder_name", folder_name)
            .eq("is_deleted", False)
        )

        if parent_folder_id:
            query = query.eq("parent_folder_id", parent_folder_id)
        else:
            query = query.is_("parent_folder_id", None)

        existing = query.execute()
        if existing.data:
            raise ValueError(f"Folder '{folder_name}' already exists at this location")

        # Create folder
        result = (
            supabase.table("student_folders")
            .insert(
                {
                    "student_id": str(student_id),
                    "folder_name": folder_name,
                    "parent_folder_id": parent_folder_id,
                    "description": description,
                    "is_deleted": False,
                }
            )
            .execute()
        )

        if not result.data:
            raise RuntimeError("Failed to create folder")

        return result.data[0]

    @staticmethod
    async def list_folders(student_id: UUID, parent_folder_id: Optional[int] = None) -> list[dict[str, Any]]:
        """List folders for a student at specified parent level."""
        supabase = StudentFileService._supabase()

        query = (
            supabase.table("student_folders")
            .select("*")
            .eq("student_id", str(student_id))
            .eq("is_deleted", False)
            .order("folder_name", desc=False)
        )

        if parent_folder_id is not None:
            query = query.eq("parent_folder_id", parent_folder_id)
        else:
            query = query.is_("parent_folder_id", None)

        result = query.execute()
        return result.data or []

    @staticmethod
    async def get_folder(folder_id: int, student_id: UUID) -> dict[str, Any]:
        """Get folder details with authorization check."""
        supabase = StudentFileService._supabase()

        result = (
            supabase.table("student_folders")
            .select("*")
            .eq("id", folder_id)
            .eq("student_id", str(student_id))
            .eq("is_deleted", False)
            .single()
            .execute()
        )

        if not result.data:
            raise ValueError(f"Folder {folder_id} not found")

        return result.data

    @staticmethod
    async def update_folder(
        folder_id: int,
        student_id: UUID,
        folder_name: Optional[str] = None,
        description: Optional[str] = None,
        parent_folder_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Update folder metadata."""
        supabase = StudentFileService._supabase()

        # Verify ownership
        await StudentFileService.get_folder(folder_id, student_id)

        # Prevent circular parent references
        if parent_folder_id and parent_folder_id == folder_id:
            raise ValueError("Cannot set folder as its own parent")

        update_data = {"updated_at": datetime.utcnow().isoformat()}

        if folder_name is not None:
            update_data["folder_name"] = folder_name
        if description is not None:
            update_data["description"] = description
        if parent_folder_id is not None:
            update_data["parent_folder_id"] = parent_folder_id

        result = (
            supabase.table("student_folders")
            .update(update_data)
            .eq("id", folder_id)
            .eq("student_id", str(student_id))
            .execute()
        )

        if not result.data:
            raise RuntimeError("Failed to update folder")

        return result.data[0]

    @staticmethod
    async def delete_folder(folder_id: int, student_id: UUID, force: bool = False) -> None:
        """Delete folder (soft delete by default)."""
        supabase = StudentFileService._supabase()

        # Check folder exists and belongs to student
        await StudentFileService.get_folder(folder_id, student_id)

        # Check if folder has files
        if not force:
            files = (
                supabase.table("student_files")
                .select("id")
                .eq("folder_id", folder_id)
                .eq("is_deleted", False)
                .execute()
            )
            if files.data:
                raise ValueError(f"Folder contains {len(files.data)} file(s). Delete files first or use force=True")

        # Soft delete
        supabase.table("student_folders").update(
            {"is_deleted": True, "updated_at": datetime.utcnow().isoformat()}
        ).eq("id", folder_id).execute()

    # ========================================================================
    # FILE OPERATIONS
    # ========================================================================

    @staticmethod
    async def create_file(
        student_id: UUID,
        file_name: str,
        file_title: str,
        file_url: str,
        mime_type: Optional[str] = None,
        size_bytes: Optional[int] = None,
        storage_provider: str = "cloudinary",
        cloudinary_public_id: Optional[str] = None,
        supabase_storage_path: Optional[str] = None,
        folder_id: Optional[int] = None,
        description: Optional[str] = None,
        uploaded_by: Optional[UUID] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Create file metadata record."""
        supabase = StudentFileService._supabase()

        # Validate storage provider
        if storage_provider not in ("cloudinary", "supabase"):
            raise ValueError(f"Invalid storage_provider: {storage_provider}")

        # Validate folder if provided
        if folder_id:
            folder = (
                supabase.table("student_folders")
                .select("id, student_id")
                .eq("id", folder_id)
                .eq("is_deleted", False)
                .single()
                .execute()
            )
            if not folder.data or folder.data.get("student_id") != str(student_id):
                raise ValueError(f"Folder {folder_id} not found or access denied")

        # Check storage quota
        used = await StudentFileService.get_storage_used(student_id)
        if used + (size_bytes or 0) > StudentFileService.STUDENT_QUOTA_BYTES:
            raise ValueError(
                f"Storage quota exceeded. Used: {used} bytes, "
                f"file: {size_bytes} bytes, quota: {StudentFileService.STUDENT_QUOTA_BYTES} bytes"
            )

        result = (
            supabase.table("student_files")
            .insert(
                {
                    "student_id": str(student_id),
                    "file_name": file_name,
                    "file_title": file_title,
                    "file_url": file_url,
                    "mime_type": mime_type,
                    "size_bytes": size_bytes,
                    "storage_provider": storage_provider,
                    "cloudinary_public_id": cloudinary_public_id,
                    "supabase_storage_path": supabase_storage_path,
                    "folder_id": folder_id,
                    "description": description,
                    "uploaded_by": str(uploaded_by) if uploaded_by else str(student_id),
                    "is_deleted": False,
                    "metadata": metadata or {},
                }
            )
            .execute()
        )

        if not result.data:
            raise RuntimeError("Failed to create file record")

        return result.data[0]

    @staticmethod
    async def list_files(
        student_id: UUID, folder_id: Optional[int] = None, limit: int = 500, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """List files for student in folder."""
        supabase = StudentFileService._supabase()

        # Count total
        count_query = (
            supabase.table("student_files")
            .select("*", count="exact")
            .eq("student_id", str(student_id))
            .eq("is_deleted", False)
        )

        if folder_id is not None:
            count_query = count_query.eq("folder_id", folder_id)
        else:
            count_query = count_query.is_("folder_id", None)

        count_result = count_query.execute()
        total = count_result.count or 0

        # Get paginated data
        query = (
            supabase.table("student_files")
            .select("*")
            .eq("student_id", str(student_id))
            .eq("is_deleted", False)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
        )

        if folder_id is not None:
            query = query.eq("folder_id", folder_id)
        else:
            query = query.is_("folder_id", None)

        result = query.execute()
        return result.data or [], total

    @staticmethod
    async def get_file(file_id: int, student_id: UUID) -> dict[str, Any]:
        """Get file details with authorization check."""
        supabase = StudentFileService._supabase()

        result = (
            supabase.table("student_files")
            .select("*")
            .eq("id", file_id)
            .eq("student_id", str(student_id))
            .eq("is_deleted", False)
            .single()
            .execute()
        )

        if not result.data:
            raise ValueError(f"File {file_id} not found")

        return result.data

    @staticmethod
    async def update_file(
        file_id: int,
        student_id: UUID,
        file_title: Optional[str] = None,
        description: Optional[str] = None,
        folder_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Update file metadata (not content)."""
        supabase = StudentFileService._supabase()

        # Verify ownership
        await StudentFileService.get_file(file_id, student_id)

        # Validate new folder if provided
        if folder_id is not None:
            folder = (
                supabase.table("student_folders")
                .select("id, student_id")
                .eq("id", folder_id)
                .eq("is_deleted", False)
                .single()
                .execute()
            )
            if not folder.data or folder.data.get("student_id") != str(student_id):
                raise ValueError(f"Folder {folder_id} not found or access denied")

        update_data = {"updated_at": datetime.utcnow().isoformat()}

        if file_title is not None:
            update_data["file_title"] = file_title
        if description is not None:
            update_data["description"] = description
        if folder_id is not None:
            update_data["folder_id"] = folder_id

        result = (
            supabase.table("student_files")
            .update(update_data)
            .eq("id", file_id)
            .eq("student_id", str(student_id))
            .execute()
        )

        if not result.data:
            raise RuntimeError("Failed to update file")

        return result.data[0]

    @staticmethod
    async def delete_file(file_id: int, student_id: UUID) -> None:
        """Delete file (soft delete)."""
        supabase = StudentFileService._supabase()

        # Check file exists and belongs to student
        file_data = await StudentFileService.get_file(file_id, student_id)

        # Delete from cloud storage
        storage_provider = file_data.get("storage_provider")
        if storage_provider == "cloudinary":
            from app.services.storage.cloudinary_service import delete_public_id, public_id_from_url

            cloudinary_id = file_data.get("cloudinary_public_id") or public_id_from_url(file_data.get("file_url"))
            if cloudinary_id:
                for resource_type in StudentFileService._cloudinary_resource_type_candidates(file_data):
                    try:
                        result = delete_public_id(cloudinary_id, resource_type=resource_type)
                        status = str((result or {}).get("result") or "").lower()
                        if status == "ok":
                            break
                    except Exception:
                        continue
        # TODO: Add Supabase bucket deletion if needed

        # Soft delete in database
        supabase.table("student_files").update(
            {"is_deleted": True, "updated_at": datetime.utcnow().isoformat()}
        ).eq("id", file_id).execute()

    # ========================================================================
    # STORAGE QUOTA OPERATIONS
    # ========================================================================

    @staticmethod
    async def get_storage_used(student_id: UUID) -> int:
        """Get total storage used by student in bytes."""
        supabase = StudentFileService._supabase()

        result = (
            supabase.table("student_storage_usage")
            .select("total_bytes")
            .eq("student_id", str(student_id))
            .execute()
        )

        if result.data:
            row = result.data[0]
            return int(row.get("total_bytes", 0))

        return 0

    @staticmethod
    async def get_storage_usage(student_id: UUID) -> dict[str, Any]:
        """Get detailed storage usage for student."""
        supabase = StudentFileService._supabase()

        result = (
            supabase.table("student_storage_usage")
            .select("*")
            .eq("student_id", str(student_id))
            .execute()
        )

        data = result.data[0] if result.data else None
        if data:
            return {
                "student_id": str(student_id),
                "used_bytes": int(data.get("total_bytes", 0)),
                "quota_bytes": StudentFileService.STUDENT_QUOTA_BYTES,
                "remaining_bytes": max(
                    StudentFileService.STUDENT_QUOTA_BYTES - int(data.get("total_bytes", 0)), 0
                ),
                "file_count": int(data.get("file_count", 0)),
            }

        return {
            "student_id": str(student_id),
            "used_bytes": 0,
            "quota_bytes": StudentFileService.STUDENT_QUOTA_BYTES,
            "remaining_bytes": StudentFileService.STUDENT_QUOTA_BYTES,
            "file_count": 0,
        }

    # ========================================================================
    # UTILITY OPERATIONS
    # ========================================================================

    @staticmethod
    async def get_folder_path(folder_id: Optional[int], student_id: UUID) -> str:
        """Get folder breadcrumb path (e.g., 'Main / Documents / School')."""
        if not folder_id:
            return "Main"

        supabase = StudentFileService._supabase()
        path_parts = []

        current_id = folder_id
        while current_id:
            result = (
                supabase.table("student_folders")
                .select("id, folder_name, parent_folder_id")
                .eq("id", current_id)
                .eq("student_id", str(student_id))
                .eq("is_deleted", False)
                .single()
                .execute()
            )

            if not result.data:
                break

            path_parts.insert(0, result.data["folder_name"])
            current_id = result.data.get("parent_folder_id")

        return "Main / " + " / ".join(path_parts) if path_parts else "Main"
