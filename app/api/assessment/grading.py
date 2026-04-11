"""Grading endpoints for lecturer/admin review."""

from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import require_roles
from app.models.assignment import SubmissionFeedbackIn, SubmissionGradeIn, SubmissionOut
from app.services.assessment.grading_service import apply_manual_grades
from app.api.assessment.assignments import (
    _can_manage_assignment,
    _get_assignment,
    _sb,
    _submission_to_out,
)

router = APIRouter(prefix="/grading", tags=["Learning - Grading"])


def _get_submission(sb, submission_id: int) -> dict:
    res = sb.table("assignment_submissions").select("*").eq("id", submission_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Submission not found")
    return res.data[0]


@router.post("/{submission_id}/grade", response_model=SubmissionOut)
async def grade_submission(
    submission_id: int,
    payload: SubmissionGradeIn,
    user: dict = Depends(require_roles(["Admin", "Lecturer"])),
):
    """Lecturer/admin grades essay or mixed submissions."""
    sb = _sb()
    submission = _get_submission(sb, submission_id)
    assignment = _get_assignment(sb, submission["assignment_id"])
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if not _can_manage_assignment(user, sb, assignment):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        updated = apply_manual_grades(
            sb,
            submission_row=submission,
            assignment_row=assignment,
            answer_grades=[grade.model_dump(exclude_unset=True) for grade in payload.answer_grades],
            feedback=payload.feedback,
            finalize=payload.finalize,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _submission_to_out(sb, updated, True)


@router.post("/{submission_id}/feedback", response_model=SubmissionOut)
async def add_feedback(
    submission_id: int,
    payload: SubmissionFeedbackIn,
    user: dict = Depends(require_roles(["Admin", "Lecturer"])),
):
    """Lecturer/admin adds overall submission feedback without grading answers."""
    sb = _sb()
    submission = _get_submission(sb, submission_id)
    assignment = _get_assignment(sb, submission["assignment_id"])
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if not _can_manage_assignment(user, sb, assignment):
        raise HTTPException(status_code=403, detail="Forbidden")

    updated = (
        sb.table("assignment_submissions")
        .update({"feedback": payload.feedback})
        .eq("id", submission_id)
        .execute()
    )
    if not updated.data:
        raise HTTPException(status_code=500, detail="Failed to save feedback")
    return _submission_to_out(sb, updated.data[0], True)
