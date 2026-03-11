"""Assignments - Create, deadlines, notifications, submissions."""

from fastapi import APIRouter, Depends, UploadFile

from app.api.deps import DbDep

router = APIRouter(prefix="/assignments", tags=["Learning - Assignments"])


@router.post("/")
async def create_assignment(db: DbDep):
    """Lecturer: Create assignment with deadline and requirements."""
    ...


@router.get("/")
async def list_assignments(db: DbDep):
    """List assignments (filtered by role)."""
    ...


@router.post("/{assignment_id}/submit")
async def submit_assignment(db: DbDep, assignment_id: str, file: UploadFile):
    """Student: Submit assignment (with early/late timestamp tracking)."""
    ...


@router.post("/{assignment_id}/notify")
async def send_deadline_reminder(db: DbDep, assignment_id: str):
    """Send deadline reminder notifications."""
    ...
