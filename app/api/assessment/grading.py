"""Grading - Essays, quizzes, feedback."""

from fastapi import APIRouter, Depends

from app.api.deps import DbDep

router = APIRouter(prefix="/grading", tags=["Learning - Grading"])


@router.post("/{submission_id}/grade")
async def grade_submission(db: DbDep, submission_id: str):
    """Lecturer: Grade essay/manual submission; quizzes auto-graded on creation."""
    ...


@router.post("/{submission_id}/feedback")
async def add_feedback(db: DbDep, submission_id: str):
    """Lecturer: Add feedback and comments to submission."""
    ...
