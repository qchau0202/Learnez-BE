"""Pydantic models for student file management (CRUD schemas)."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ============================================================================
# FOLDER MODELS
# ============================================================================
class StudentFolderBase(BaseModel):
    """Base folder schema."""

    folder_name: str = Field(..., min_length=1, max_length=255, description="Folder display name")
    description: Optional[str] = Field(None, max_length=1000, description="Optional folder description")
    parent_folder_id: Optional[int] = Field(None, description="Parent folder ID for nested structure; NULL = Main folder")


class StudentFolderCreate(StudentFolderBase):
    """Create folder request."""

    pass


class StudentFolderUpdate(BaseModel):
    """Update folder request."""

    folder_name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1000)
    parent_folder_id: Optional[int] = Field(None, description="Move folder to different parent")


class StudentFolderResponse(StudentFolderBase):
    """Folder response schema."""

    id: int
    student_id: str  # UUID as string
    created_at: datetime
    updated_at: datetime
    metadata: Optional[dict[str, Any]] = None

    model_config = {"from_attributes": True}


class StudentFolderDetailResponse(StudentFolderResponse):
    """Folder with file count details."""

    file_count: Optional[int] = Field(None, description="Number of files in this folder")


# ============================================================================
# FILE MODELS
# ============================================================================
class StudentFileBase(BaseModel):
    """Base file schema."""

    file_title: str = Field(..., min_length=1, max_length=500, description="Display name for the file")
    description: Optional[str] = Field(None, max_length=2000, description="Optional file description")
    folder_id: Optional[int] = Field(None, description="Folder ID; NULL = Main folder")


class StudentFileCreate(StudentFileBase):
    """Create file request (used during upload)."""

    pass


class StudentFileUpdate(BaseModel):
    """Update file metadata request (not content)."""

    file_title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=2000)
    folder_id: Optional[int] = Field(None, description="Move file to different folder")


class StudentFileResponse(StudentFileBase):
    """File response schema."""

    id: int
    student_id: str  # UUID as string
    file_name: str = Field(description="Actual filename in storage (may include uuid prefix)")
    mime_type: Optional[str]
    size_bytes: Optional[int]
    storage_provider: str = Field(default="cloudinary", description="'cloudinary' or 'supabase'")
    cloudinary_public_id: Optional[str] = None
    supabase_storage_path: Optional[str] = None
    file_url: Optional[str] = Field(None, description="Signed URL for download")
    created_at: datetime
    updated_at: datetime
    uploaded_by: Optional[str] = None  # UUID as string
    metadata: Optional[dict[str, Any]] = None

    model_config = {"from_attributes": True}


class StudentFileDetailResponse(StudentFileResponse):
    """File with full details including folder location."""

    folder_location: Optional[str] = Field(None, description="Breadcrumb-like path: 'Main / Folder / SubFolder'")


# ============================================================================
# STORAGE USAGE MODELS
# ============================================================================
class StorageUsageResponse(BaseModel):
    """Storage quota and usage information."""

    student_id: str
    used_bytes: int
    quota_bytes: int
    remaining_bytes: int
    used_mb: float
    quota_mb: float
    file_count: int = Field(default=0, description="Number of non-deleted files")

    model_config = {"from_attributes": True}


# ============================================================================
# LIST/PAGINATION MODELS
# ============================================================================
class FolderListResponse(BaseModel):
    """List of folders response."""

    items: list[StudentFolderDetailResponse]
    total: int


class FileListResponse(BaseModel):
    """List of files response."""

    items: list[StudentFileResponse]
    total: int
    folder_id: Optional[int] = None
    storage_used_mb: float


# ============================================================================
# BULK OPERATIONS
# ============================================================================
class BulkDeleteRequest(BaseModel):
    """Bulk delete request for files or folders."""

    ids: list[int] = Field(..., min_items=1, max_items=100, description="IDs to delete")
    resource_type: str = Field(
        ..., pattern="^(file|folder)$", description="Type of resource: 'file' or 'folder'"
    )
