"""Attendance - QR Code + Geolocation anti-cheat."""

from fastapi import APIRouter, Depends

from app.api.deps import DbDep

router = APIRouter(prefix="/attendance", tags=["Learning - Attendance"])


@router.post("/session")
async def create_attendance_session(db: DbDep):
    """Lecturer: Create QR-based attendance session."""
    ...


@router.post("/check-in")
async def check_in(db: DbDep):
    """Student: Check in via QR + GPS coordinates for anti-cheat."""
    ...


@router.get("/{session_id}")
async def get_attendance(db: DbDep, session_id: str):
    """Lecturer: View attendance results."""
    ...
