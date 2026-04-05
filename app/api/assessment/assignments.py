"""Assignments CRUD, questions, and submissions (Supabase) — LMS-style RBAC.

DB note: ``assignment_submission_answers`` should allow many rows per submission
(one per question). Use a composite unique (submission_id, question_id), not
separate UNIQUE constraints on each column alone.
"""

from __future__ import annotations

from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, require_roles
from app.services.assignment_cascade import delete_assignment_cascade
from app.models.assignment import (
    AnswerIn,
    AssignmentCreate,
    AssignmentDetailOut,
    AssignmentOut,
    AssignmentUpdate,
    QuestionCreate,
    QuestionOut,
    QuestionUpdate,
    SubmissionCreate,
    SubmissionOut,
    SubmissionUpdate,
)

router = APIRouter(prefix="/assignments", tags=["Learning - Assignments"])


def _sb():
    supabase = get_supabase(service_role=True)
    if not supabase:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")
    return supabase


def _get_module(sb, module_id: int) -> dict | None:
    r = sb.table("modules").select("*").eq("id", module_id).limit(1).execute()
    return r.data[0] if r.data else None


def _get_course(sb, course_id: int) -> dict | None:
    r = sb.table("courses").select("*").eq("id", course_id).limit(1).execute()
    return r.data[0] if r.data else None


def _lecturer_owns_module(sb, user_id: str, module_id: int | None) -> bool:
    if module_id is None:
        return False
    mod = _get_module(sb, module_id)
    if not mod or not mod.get("course_id"):
        return False
    c = _get_course(sb, mod["course_id"])
    return bool(c and c.get("lecturer_id") == user_id)


def _module_ids_for_lecturer(sb, uid: str) -> list[int]:
    crs = sb.table("courses").select("id").eq("lecturer_id", uid).execute()
    cids = [r["id"] for r in (crs.data or [])]
    if not cids:
        return []
    mods = sb.table("modules").select("id").in_("course_id", cids).execute()
    return [r["id"] for r in (mods.data or [])]


def _module_ids_for_student(sb, uid: str) -> list[int]:
    enr = sb.table("course_enrollments").select("course_id").eq("student_id", uid).execute()
    cids = [r["course_id"] for r in (enr.data or [])]
    if not cids:
        return []
    mods = sb.table("modules").select("id").in_("course_id", cids).execute()
    return [r["id"] for r in (mods.data or [])]


def _get_assignment(sb, assignment_id: int) -> dict | None:
    r = sb.table("assignments").select("*").eq("id", assignment_id).limit(1).execute()
    return r.data[0] if r.data else None


def _can_view_assignment(user: dict[str, Any], sb, row: dict) -> bool:
    role = ROLE_MAP.get(user["role_id"])
    if role == "Admin":
        return True
    mid = row.get("module_id")
    if role == "Lecturer":
        return _lecturer_owns_module(sb, user["user_id"], mid)
    if role == "Student":
        return mid in _module_ids_for_student(sb, user["user_id"])
    return False


def _can_manage_assignment(user: dict[str, Any], sb, row: dict) -> bool:
    role = ROLE_MAP.get(user["role_id"])
    if role == "Admin":
        return True
    if role == "Lecturer":
        return _lecturer_owns_module(sb, user["user_id"], row.get("module_id"))
    return False


def _question_ids_for_assignment(sb, assignment_id: int) -> set[int]:
    q = sb.table("assignment_questions").select("id").eq("assignment_id", assignment_id).execute()
    return {r["id"] for r in (q.data or [])}


def _replace_submission_answers(
    sb,
    submission_id: int,
    answers: List[AnswerIn],
    allowed_question_ids: set[int],
):
    for a in answers:
        if a.question_id not in allowed_question_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Question {a.question_id} does not belong to this assignment",
            )
    sb.table("assignment_submission_answers").delete().eq("submission_id", submission_id).execute()
    if not answers:
        return
    rows = [
        {
            "submission_id": submission_id,
            "question_id": a.question_id,
            "answer_content": a.answer_content,
        }
        for a in answers
    ]
    try:
        sb.table("assignment_submission_answers").insert(rows).execute()
    except Exception as e:
        msg = str(e)
        if "23505" in msg:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot store multiple answers: assignment_submission_answers has UNIQUE(submission_id) "
                    "or UNIQUE(question_id). Apply BE/sql/fix_assignment_submission_answers_unique.sql "
                    "so the table uses UNIQUE(submission_id, question_id) instead."
                ),
            ) from e
        raise


def _submission_to_out(sb, sub: dict, with_answers: bool) -> dict:
    row = dict(sub)
    row["answers"] = []
    if with_answers:
        ans = (
            sb.table("assignment_submission_answers")
            .select("*")
            .eq("submission_id", sub["id"])
            .execute()
        )
        row["answers"] = ans.data or []
    return row


def _resolve_submit_student_id(user: dict[str, Any], payload: SubmissionCreate) -> str:
    role = ROLE_MAP.get(user["role_id"])
    if role == "Student":
        return user["user_id"]
    if role == "Admin":
        if not payload.student_id:
            raise HTTPException(
                status_code=400,
                detail="Admin must pass student_id when creating a submission",
            )
        return payload.student_id
    raise HTTPException(status_code=403, detail="Only Student or Admin may submit")


def _student_enrolled_for_assignment(sb, student_id: str, assignment_row: dict) -> bool:
    mod = _get_module(sb, assignment_row.get("module_id"))
    if not mod or not mod.get("course_id"):
        return False
    enr = (
        sb.table("course_enrollments")
        .select("course_id")
        .eq("course_id", mod["course_id"])
        .eq("student_id", student_id)
        .limit(1)
        .execute()
    )
    return bool(enr.data)


def _can_edit_submission(user: dict[str, Any], sub: dict) -> bool:
    if ROLE_MAP.get(user["role_id"]) == "Admin":
        return True
    if sub.get("is_corrected"):
        return False
    return sub.get("student_id") == user.get("user_id")


@router.post("/", response_model=AssignmentOut, status_code=status.HTTP_201_CREATED)
async def create_assignment(
    payload: AssignmentCreate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    role = ROLE_MAP.get(user["role_id"])
    if role == "Lecturer" and not _lecturer_owns_module(sb, user["user_id"], payload.module_id):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not _get_module(sb, payload.module_id):
        raise HTTPException(status_code=404, detail="Module not found")

    row = {
        "module_id": payload.module_id,
        "title": payload.title,
        "description": payload.description,
        "due_date": payload.due_date.isoformat() if payload.due_date else None,
        "total_score": payload.total_score,
        "is_graded": payload.is_graded,
        "uploaded_by": user["user_id"],
    }
    row = {k: v for k, v in row.items() if v is not None}
    created = sb.table("assignments").insert(row).execute()
    if not created.data:
        raise HTTPException(status_code=500, detail="Failed to create assignment")
    aid = created.data[0]["id"]

    if payload.questions:
        for i, q in enumerate(payload.questions):
            qb = q.model_dump(exclude_unset=True)
            qb["assignment_id"] = aid
            if qb.get("order_index") is None:
                qb["order_index"] = i
            sb.table("assignment_questions").insert(qb).execute()

    return created.data[0]


@router.get("/", response_model=List[AssignmentOut])
async def list_assignments(
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    role = ROLE_MAP.get(user["role_id"])
    if role == "Admin":
        res = sb.table("assignments").select("*").order("id", desc=True).execute()
        return res.data or []
    if role == "Lecturer":
        mids = _module_ids_for_lecturer(sb, user["user_id"])
        if not mids:
            return []
        res = sb.table("assignments").select("*").in_("module_id", mids).order("id", desc=True).execute()
        return res.data or []
    mids = _module_ids_for_student(sb, user["user_id"])
    if not mids:
        return []
    res = sb.table("assignments").select("*").in_("module_id", mids).order("id", desc=True).execute()
    return res.data or []


@router.get("/{assignment_id}", response_model=AssignmentDetailOut)
async def get_assignment(
    assignment_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    row = _get_assignment(sb, assignment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if not _can_view_assignment(user, sb, row):
        raise HTTPException(status_code=403, detail="Forbidden")
    qs = (
        sb.table("assignment_questions")
        .select("*")
        .eq("assignment_id", assignment_id)
        .order("id")
        .execute()
    )
    return {**row, "questions": qs.data or []}


@router.put("/{assignment_id}", response_model=AssignmentOut)
async def update_assignment(
    assignment_id: int,
    payload: AssignmentUpdate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    row = _get_assignment(sb, assignment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if not _can_manage_assignment(user, sb, row):
        raise HTTPException(status_code=403, detail="Forbidden")

    data = payload.model_dump(exclude_unset=True)
    if "module_id" in data and data["module_id"] is not None:
        if ROLE_MAP.get(user["role_id"]) == "Lecturer":
            if not _lecturer_owns_module(sb, user["user_id"], data["module_id"]):
                raise HTTPException(status_code=403, detail="Forbidden")
        if not _get_module(sb, data["module_id"]):
            raise HTTPException(status_code=404, detail="Module not found")
    if "due_date" in data and data["due_date"] is not None:
        data["due_date"] = data["due_date"].isoformat()
    if not data:
        return row

    updated = sb.table("assignments").update(data).eq("id", assignment_id).execute()
    if not updated.data:
        raise HTTPException(status_code=500, detail="Failed to update assignment")
    return updated.data[0]


@router.delete("/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assignment(
    assignment_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    row = _get_assignment(sb, assignment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if not _can_manage_assignment(user, sb, row):
        raise HTTPException(status_code=403, detail="Forbidden")
    delete_assignment_cascade(sb, assignment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{assignment_id}/questions",
    response_model=QuestionOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_question(
    assignment_id: int,
    payload: QuestionCreate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    row = _get_assignment(sb, assignment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if not _can_manage_assignment(user, sb, row):
        raise HTTPException(status_code=403, detail="Forbidden")
    qb = payload.model_dump(exclude_unset=True)
    qb["assignment_id"] = assignment_id
    ins = sb.table("assignment_questions").insert(qb).execute()
    if not ins.data:
        raise HTTPException(status_code=500, detail="Failed to create question")
    return ins.data[0]


@router.put("/{assignment_id}/questions/{question_id}", response_model=QuestionOut)
async def update_question(
    assignment_id: int,
    question_id: int,
    payload: QuestionUpdate,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    row = _get_assignment(sb, assignment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if not _can_manage_assignment(user, sb, row):
        raise HTTPException(status_code=403, detail="Forbidden")
    qq = (
        sb.table("assignment_questions")
        .select("*")
        .eq("id", question_id)
        .eq("assignment_id", assignment_id)
        .limit(1)
        .execute()
    )
    if not qq.data:
        raise HTTPException(status_code=404, detail="Question not found")
    data = payload.model_dump(exclude_unset=True)
    if not data:
        return qq.data[0]
    upd = (
        sb.table("assignment_questions")
        .update(data)
        .eq("id", question_id)
        .eq("assignment_id", assignment_id)
        .execute()
    )
    if not upd.data:
        raise HTTPException(status_code=500, detail="Failed to update question")
    return upd.data[0]


@router.delete(
    "/{assignment_id}/questions/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_question(
    assignment_id: int,
    question_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer"])),
):
    sb = _sb()
    row = _get_assignment(sb, assignment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Assignment not found")
    if not _can_manage_assignment(user, sb, row):
        raise HTTPException(status_code=403, detail="Forbidden")
    sb.table("assignment_questions").delete().eq("id", question_id).eq("assignment_id", assignment_id).execute()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{assignment_id}/submissions",
    response_model=SubmissionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_or_resubmit(
    assignment_id: int,
    payload: SubmissionCreate,
    user: dict[str, Any] = Depends(require_roles(["Student", "Admin"])),
):
    sb = _sb()
    arow = _get_assignment(sb, assignment_id)
    if not arow:
        raise HTTPException(status_code=404, detail="Assignment not found")

    role = ROLE_MAP.get(user["role_id"])
    if role == "Student" and not _can_view_assignment(user, sb, arow):
        raise HTTPException(status_code=403, detail="Forbidden")

    student_id = _resolve_submit_student_id(user, payload)
    if ROLE_MAP.get(user["role_id"]) == "Student":
        if not _student_enrolled_for_assignment(sb, student_id, arow):
            raise HTTPException(status_code=403, detail="You are not enrolled in this course")

    qids = _question_ids_for_assignment(sb, assignment_id)
    existing = (
        sb.table("assignment_submissions")
        .select("*")
        .eq("assignment_id", assignment_id)
        .eq("student_id", student_id)
        .limit(1)
        .execute()
    )

    if existing.data:
        sub = existing.data[0]
        if sub.get("is_corrected"):
            raise HTTPException(status_code=400, detail="Submission is already graded; cannot change")
        if not _can_edit_submission(user, sub):
            raise HTTPException(status_code=403, detail="Forbidden")
        sb.table("assignment_submissions").update({"status": payload.status}).eq("id", sub["id"]).execute()
        _replace_submission_answers(sb, sub["id"], payload.answers, qids)
        sub2 = (
            sb.table("assignment_submissions")
            .select("*")
            .eq("id", sub["id"])
            .limit(1)
            .execute()
        ).data[0]
        return _submission_to_out(sb, sub2, True)

    ins = (
        sb.table("assignment_submissions")
        .insert(
            {
                "student_id": student_id,
                "assignment_id": assignment_id,
                "status": payload.status,
                "is_corrected": False,
            }
        )
        .execute()
    )
    if not ins.data:
        raise HTTPException(status_code=500, detail="Failed to create submission")
    sid = ins.data[0]["id"]
    try:
        _replace_submission_answers(sb, sid, payload.answers, qids)
    except HTTPException:
        sb.table("assignment_submissions").delete().eq("id", sid).execute()
        raise
    except Exception:
        sb.table("assignment_submissions").delete().eq("id", sid).execute()
        raise
    sub3 = sb.table("assignment_submissions").select("*").eq("id", sid).limit(1).execute().data[0]
    return _submission_to_out(sb, sub3, True)


@router.get("/{assignment_id}/submissions", response_model=List[SubmissionOut])
async def list_submissions(
    assignment_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    arow = _get_assignment(sb, assignment_id)
    if not arow:
        raise HTTPException(status_code=404, detail="Assignment not found")
    role = ROLE_MAP.get(user["role_id"])
    if role == "Student":
        if not _can_view_assignment(user, sb, arow):
            raise HTTPException(status_code=403, detail="Forbidden")
        res = (
            sb.table("assignment_submissions")
            .select("*")
            .eq("assignment_id", assignment_id)
            .eq("student_id", user["user_id"])
            .execute()
        )
        return [_submission_to_out(sb, s, False) for s in (res.data or [])]
    if not _can_view_assignment(user, sb, arow):
        raise HTTPException(status_code=403, detail="Forbidden")
    if role == "Lecturer" and not _can_manage_assignment(user, sb, arow):
        raise HTTPException(status_code=403, detail="Forbidden")
    res = sb.table("assignment_submissions").select("*").eq("assignment_id", assignment_id).execute()
    return [_submission_to_out(sb, s, False) for s in (res.data or [])]


@router.get("/{assignment_id}/submissions/{submission_id}", response_model=SubmissionOut)
async def get_submission(
    assignment_id: int,
    submission_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    arow = _get_assignment(sb, assignment_id)
    if not arow:
        raise HTTPException(status_code=404, detail="Assignment not found")
    sub = (
        sb.table("assignment_submissions")
        .select("*")
        .eq("id", submission_id)
        .eq("assignment_id", assignment_id)
        .limit(1)
        .execute()
    )
    if not sub.data:
        raise HTTPException(status_code=404, detail="Submission not found")
    srow = sub.data[0]
    role = ROLE_MAP.get(user["role_id"])
    if role == "Student":
        if srow.get("student_id") != user["user_id"]:
            raise HTTPException(status_code=403, detail="Forbidden")
        if not _can_view_assignment(user, sb, arow):
            raise HTTPException(status_code=403, detail="Forbidden")
    elif role == "Lecturer":
        if not _can_manage_assignment(user, sb, arow):
            raise HTTPException(status_code=403, detail="Forbidden")
    return _submission_to_out(sb, srow, True)


@router.put("/{assignment_id}/submissions/{submission_id}", response_model=SubmissionOut)
async def update_submission(
    assignment_id: int,
    submission_id: int,
    payload: SubmissionUpdate,
    user: dict[str, Any] = Depends(require_roles(["Student", "Admin"])),
):
    sb = _sb()
    arow = _get_assignment(sb, assignment_id)
    if not arow:
        raise HTTPException(status_code=404, detail="Assignment not found")
    sub = (
        sb.table("assignment_submissions")
        .select("*")
        .eq("id", submission_id)
        .eq("assignment_id", assignment_id)
        .limit(1)
        .execute()
    )
    if not sub.data:
        raise HTTPException(status_code=404, detail="Submission not found")
    srow = sub.data[0]
    if not _can_edit_submission(user, srow):
        raise HTTPException(status_code=403, detail="Forbidden")
    if ROLE_MAP.get(user["role_id"]) == "Student" and not _can_view_assignment(user, sb, arow):
        raise HTTPException(status_code=403, detail="Forbidden")

    data = payload.model_dump(exclude_unset=True)
    answers = data.pop("answers", None)
    if data:
        sb.table("assignment_submissions").update(data).eq("id", submission_id).execute()
    if answers is not None:
        qids = _question_ids_for_assignment(sb, assignment_id)
        answer_models = [AnswerIn(**a) if isinstance(a, dict) else a for a in answers]
        _replace_submission_answers(sb, submission_id, answer_models, qids)
    s2 = sb.table("assignment_submissions").select("*").eq("id", submission_id).limit(1).execute().data[0]
    return _submission_to_out(sb, s2, True)


@router.delete(
    "/{assignment_id}/submissions/{submission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_submission(
    assignment_id: int,
    submission_id: int,
    user: dict[str, Any] = Depends(require_roles(["Student", "Admin"])),
):
    sb = _sb()
    arow = _get_assignment(sb, assignment_id)
    if not arow:
        raise HTTPException(status_code=404, detail="Assignment not found")
    sub = (
        sb.table("assignment_submissions")
        .select("*")
        .eq("id", submission_id)
        .eq("assignment_id", assignment_id)
        .limit(1)
        .execute()
    )
    if not sub.data:
        raise HTTPException(status_code=404, detail="Submission not found")
    srow = sub.data[0]
    if not _can_edit_submission(user, srow):
        raise HTTPException(status_code=403, detail="Forbidden")
    if ROLE_MAP.get(user["role_id"]) == "Student" and not _can_view_assignment(user, sb, arow):
        raise HTTPException(status_code=403, detail="Forbidden")
    sb.table("assignment_submission_answers").delete().eq("submission_id", submission_id).execute()
    sb.table("assignment_submissions").delete().eq("id", submission_id).execute()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
