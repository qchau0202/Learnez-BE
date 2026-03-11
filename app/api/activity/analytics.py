"""Analytics - Competency, Learning Path, Dropout Risk."""

from fastapi import APIRouter, Depends

from app.api.deps import DbDep

router = APIRouter(prefix="/analytics", tags=["AI Analytics"])


@router.get("/{student_id}/competency")
async def get_competency_analysis(db: DbDep, student_id: str):
    """AI preliminary assessment: strengths/weaknesses from assignments & quizzes."""
    ...


@router.get("/{student_id}/learning-path")
async def get_learning_path(db: DbDep, student_id: str):
    """Agentic AI: Personalized learning path from real-time data."""
    ...


@router.get("/{student_id}/dropout-risk")
async def get_dropout_risk(db: DbDep, student_id: str):
    """Classify student: Low / Medium / High dropout risk."""
    ...
