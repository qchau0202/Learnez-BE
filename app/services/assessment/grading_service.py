"""Grading helpers for MCQ auto-grade and manual lecturer grading."""

from __future__ import annotations

import json
from typing import Any


def _question_type(question: dict[str, Any]) -> str:
    return str(question.get("type") or "").strip().lower()


def _parse_selected(answer_content: str | None) -> list[str]:
    if not answer_content:
        return []
    try:
        parsed = json.loads(answer_content)
    except (TypeError, ValueError):
        parsed = answer_content

    if isinstance(parsed, dict):
        raw = parsed.get("selected", [])
    elif isinstance(parsed, list):
        raw = parsed
    elif isinstance(parsed, str):
        raw = [parsed]
    else:
        raw = []
    return sorted({str(item) for item in raw if item is not None})


def _question_weight(assignment_row: dict[str, Any], questions: list[dict[str, Any]]) -> float:
    if not questions:
        return 0.0
    total_score = assignment_row.get("total_score")
    if total_score is None:
        return 1.0
    try:
        return float(total_score) / len(questions)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def auto_grade_submission(
    sb,
    assignment_row: dict[str, Any],
    submission_id: int,
) -> dict[str, Any]:
    questions_res = (
        sb.table("assignment_questions")
        .select("*")
        .eq("assignment_id", assignment_row["id"])
        .order("id")
        .execute()
    )
    questions = questions_res.data or []
    answers_res = (
        sb.table("assignment_submission_answers")
        .select("*")
        .eq("submission_id", submission_id)
        .execute()
    )
    answers = answers_res.data or []
    answers_by_qid = {row["question_id"]: row for row in answers}

    weight = _question_weight(assignment_row, questions)
    total = 0.0
    needs_manual_review = False

    for question in questions:
        qid = question["id"]
        answer = answers_by_qid.get(qid)
        if not answer:
            continue

        qtype = _question_type(question)
        if qtype != "mcq":
            needs_manual_review = True
            sb.table("assignment_submission_answers").update(
                {
                    "is_correct": None,
                    "earned_score": None,
                    "ai_feedback": "Pending manual grading",
                }
            ).eq("id", answer["id"]).execute()
            continue

        metadata = question.get("metadata") or {}
        correct_ids = sorted(
            {str(item) for item in (metadata.get("correct_option_ids") or []) if item is not None}
        )
        selected_ids = _parse_selected(answer.get("answer_content"))
        is_correct = selected_ids == correct_ids and bool(correct_ids or selected_ids)
        earned_score = weight if is_correct else 0.0
        total += earned_score
        feedback = "Correct answer" if is_correct else "Incorrect answer"
        sb.table("assignment_submission_answers").update(
            {
                "is_correct": is_correct,
                "earned_score": earned_score,
                "ai_feedback": feedback,
            }
        ).eq("id", answer["id"]).execute()

    submission_patch: dict[str, Any] = {
        "final_score": total,
        "is_corrected": not needs_manual_review,
    }
    sb.table("assignment_submissions").update(submission_patch).eq("id", submission_id).execute()
    refreshed = (
        sb.table("assignment_submissions")
        .select("*")
        .eq("id", submission_id)
        .limit(1)
        .execute()
    )
    return refreshed.data[0]


def apply_manual_grades(
    sb,
    submission_row: dict[str, Any],
    assignment_row: dict[str, Any],
    answer_grades: list[dict[str, Any]],
    feedback: str | None = None,
    finalize: bool = True,
) -> dict[str, Any]:
    questions_res = (
        sb.table("assignment_questions")
        .select("*")
        .eq("assignment_id", assignment_row["id"])
        .execute()
    )
    questions = questions_res.data or []
    question_ids = {row["id"] for row in questions}
    answers_res = (
        sb.table("assignment_submission_answers")
        .select("*")
        .eq("submission_id", submission_row["id"])
        .execute()
    )
    answers = answers_res.data or []
    answers_by_qid = {row["question_id"]: row for row in answers}

    total = 0.0
    for answer in answers:
        try:
            total += float(answer.get("earned_score") or 0.0)
        except (TypeError, ValueError):
            pass

    for grade in answer_grades:
        qid = grade["question_id"]
        if qid not in question_ids:
            raise ValueError(f"Question {qid} does not belong to this submission assignment")
        answer = answers_by_qid.get(qid)
        if not answer:
            raise ValueError(f"No submitted answer for question {qid}")

        previous = float(answer.get("earned_score") or 0.0)
        earned_score = float(grade["earned_score"])
        total = total - previous + earned_score
        patch = {
            "earned_score": earned_score,
            "is_correct": grade.get("is_correct"),
            "ai_feedback": grade.get("ai_feedback"),
        }
        sb.table("assignment_submission_answers").update(patch).eq("id", answer["id"]).execute()

    submission_patch: dict[str, Any] = {"final_score": total}
    if feedback is not None:
        submission_patch["feedback"] = feedback
    if finalize:
        submission_patch["is_corrected"] = True
    sb.table("assignment_submissions").update(submission_patch).eq("id", submission_row["id"]).execute()

    refreshed = (
        sb.table("assignment_submissions")
        .select("*")
        .eq("id", submission_row["id"])
        .limit(1)
        .execute()
    )
    return refreshed.data[0]
