"""Activity Tracking - Login, viewing duration, submissions."""

from fastapi import APIRouter, Depends

from app.api.deps import DbDep

router = APIRouter(prefix="/activity", tags=["Activity Tracking"])


@router.post("/log")
async def log_activity(db: DbDep):
    """Log user activity (login, document view, submission, etc.)."""
    ...


@router.get("/{user_id}")
async def get_user_activity(db: DbDep, user_id: str):
    """Get activity data for analysis and AI models."""
    ...
