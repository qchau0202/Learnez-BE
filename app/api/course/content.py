"""Content Management - Files, lecture links, Slide Preview."""

from fastapi import APIRouter, Depends, UploadFile

from app.api.deps import DbDep

router = APIRouter(prefix="/content", tags=["Course & Content - Content"])


@router.post("/upload")
async def upload_content(db: DbDep, file: UploadFile):
    """Upload file (PDF, Video, Slides) - integrates with Cloud Storage."""
    ...


@router.post("/link")
async def attach_link(db: DbDep):
    """Attach external lecture link (URL)."""
    ...


@router.get("/{content_id}/preview")
async def slide_preview(db: DbDep, content_id: str):
    """Slide Preview for faster viewing on LMS."""
    ...
