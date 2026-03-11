"""Notifications - Deadline reminders, system alerts."""

from fastapi import APIRouter, Depends

from app.api.deps import DbDep

router = APIRouter(prefix="/notifications", tags=["Learning - Notifications"])


@router.get("/")
async def list_notifications(db: DbDep):
    """Get user notifications (deadline reminders, alerts)."""
    ...


@router.post("/send")
async def send_notification(db: DbDep):
    """Trigger notification (system or third-party integration)."""
    ...
