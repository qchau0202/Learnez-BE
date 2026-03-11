"""File storage - Cloud Storage integration."""

from fastapi import APIRouter, Depends, UploadFile

from app.api.deps import DbDep

router = APIRouter(prefix="/storage", tags=["File Management"])


@router.post("/upload")
async def upload_file(db: DbDep, file: UploadFile):
    """Upload file to cloud storage (Supabase)."""
    ...


@router.get("/usage")
async def get_storage_usage(db: DbDep):
    """Get storage usage / capacity limits."""
    ...
