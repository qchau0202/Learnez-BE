"""Seed persona-aware ``assignment_submissions`` (+ per-question answers).

Idempotent: each ``(student_id, assignment_id)`` gets at most one submission.
Only seeds submissions for past-due assignments — future ones remain
unsubmitted so the lecturer's "to grade" queue still has work to demo.

Writes:
* ``assignment_submissions`` — ``status='submitted'``, ``is_corrected=true``,
  ``final_score`` on the 0-10 TDTU scale.
* ``assignment_submission_answers`` — one row per question; MCQ gets
  ``is_correct`` + ``earned_score``, essay/manual gets ``earned_score`` only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.core.database import get_supabase  # noqa: E402

from .features import PERSONAS, _resolve_courses, _resolve_student, _seeded_rng  # noqa: E402


# Per-persona grading parameters.
GRADING_PROFILE: dict[str, dict[str, Any]] = {
    "thriving": {
        "mcq_correct_prob": 0.92,
        "score_pct_range": (0.82, 0.97),
        "late_prob": 0.02,
        "elapsed_pct_range": (0.55, 0.85),
        "early_minutes_range": (90, 60 * 24),
    },
    "steady": {
        "mcq_correct_prob": 0.72,
        "score_pct_range": (0.62, 0.82),
        "late_prob": 0.10,
        "elapsed_pct_range": (0.70, 1.05),
        "early_minutes_range": (10, 60 * 12),
    },
    "at_risk": {
        "mcq_correct_prob": 0.42,
        "score_pct_range": (0.30, 0.55),
        "late_prob": 0.35,
        "elapsed_pct_range": (0.25, 0.95),
        "early_minutes_range": (-180, 30),
    },
}


def _resolve_assignments_for_student(sb, *, course_ids: list[int]) -> list[dict[str, Any]]:
    """Return assignments belonging to any of ``course_ids`` (via modules)."""
    if not course_ids:
        return []
    module_rows: list[dict[str, Any]] = []
    for i in range(0, len(course_ids), 200):
        batch = course_ids[i : i + 200]
        rows = (
            sb.table("modules").select("id, course_id").in_("course_id", batch).execute().data
            or []
        )
        module_rows.extend(rows)
    module_to_course = {
        int(m["id"]): int(m["course_id"]) for m in module_rows if m.get("id") is not None
    }
    if not module_to_course:
        return []

    assignments: list[dict[str, Any]] = []
    module_ids = sorted(module_to_course.keys())
    for i in range(0, len(module_ids), 200):
        batch = module_ids[i : i + 200]
        rows = (
            sb.table("assignments")
            .select(
                "id, title, module_id, due_date, hard_due_date, "
                "total_score, duration, duration_enabled, is_graded"
            )
            .in_("module_id", batch)
            .execute()
            .data
            or []
        )
        for r in rows:
            r["course_id"] = module_to_course.get(int(r.get("module_id") or -1))
            assignments.append(r)
    return assignments


def _resolve_questions(sb, assignment_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Group questions by ``assignment_id`` for batch grading."""
    if not assignment_ids:
        return {}
    out: dict[int, list[dict[str, Any]]] = {}
    for i in range(0, len(assignment_ids), 200):
        batch = assignment_ids[i : i + 200]
        rows = (
            sb.table("assignment_questions")
            .select("id, assignment_id, type, content, order_index, metadata")
            .in_("assignment_id", batch)
            .execute()
            .data
            or []
        )
        for r in rows:
            aid = r.get("assignment_id")
            if aid is None:
                continue
            out.setdefault(int(aid), []).append(r)
    for aid, qs in out.items():
        qs.sort(key=lambda q: int(q.get("order_index") or 0))
    return out


def _existing_submission_ids(sb, *, user_id: str, assignment_ids: list[int]) -> set[int]:
    if not assignment_ids:
        return set()
    out: set[int] = set()
    for i in range(0, len(assignment_ids), 200):
        batch = assignment_ids[i : i + 200]
        rows = (
            sb.table("assignment_submissions")
            .select("assignment_id")
            .eq("student_id", user_id)
            .in_("assignment_id", batch)
            .execute()
            .data
            or []
        )
        for r in rows:
            aid = r.get("assignment_id")
            if aid is not None:
                out.add(int(aid))
    return out


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


def _per_question_max(total_score: float | None, n_questions: int) -> float:
    if not total_score or n_questions <= 0:
        return 0.0
    return float(total_score) / n_questions


def _grade_mcq_question(question: dict[str, Any], rng) -> tuple[bool, str]:
    """Decide whether the seeded MCQ answer is correct; emit ``answer_content`` JSON."""
    metadata = question.get("metadata") or {}
    options = [opt.get("id") for opt in (metadata.get("options") or []) if opt.get("id") is not None]
    correct_ids = [str(x) for x in (metadata.get("correct_option_ids") or [])]
    if not options:
        return False, json.dumps({"selected": []})
    is_correct_pick = rng.random() < _grade_mcq_question.threshold  # type: ignore[attr-defined]
    if is_correct_pick and correct_ids:
        selected = correct_ids
    else:
        wrong_choices = [o for o in options if o not in correct_ids] or options
        selected = [rng.choice(wrong_choices)]
    return is_correct_pick, json.dumps({"selected": selected})


def seed_submissions_for_student(
    sb,
    *,
    user_id: str,
    courses: list[dict[str, Any]],
    persona_name: str,
    log_prefix: str = "",
) -> int:
    """Insert one finalised submission per past assignment, persona-aware."""
    if persona_name not in PERSONAS:
        raise ValueError(f"Unknown persona '{persona_name}'.")
    profile = GRADING_PROFILE.get(persona_name, GRADING_PROFILE["steady"])
    course_ids = [int(c["id"]) for c in courses]
    assignments = _resolve_assignments_for_student(sb, course_ids=course_ids)
    if not assignments:
        if log_prefix:
            print(f"{log_prefix}[skip] submissions: no assignments for student's courses")
        return 0

    now = datetime.now(timezone.utc)
    past_assignments = [
        a for a in assignments
        if (_parse_iso(a.get("due_date")) or now + timedelta(days=365)) <= now
    ]
    if not past_assignments:
        if log_prefix:
            print(f"{log_prefix}[skip] submissions: no past-due assignments yet")
        return 0

    aids = [int(a["id"]) for a in past_assignments]
    questions_by_aid = _resolve_questions(sb, aids)
    already_submitted = _existing_submission_ids(sb, user_id=user_id, assignment_ids=aids)

    inserted = 0
    for assignment in past_assignments:
        aid = int(assignment["id"])
        if aid in already_submitted:
            continue
        rng = _seeded_rng(persona_name, user_id, "submission", aid)
        questions = questions_by_aid.get(aid, [])
        total_score = float(assignment.get("total_score") or 0.0)
        due_date = _parse_iso(assignment.get("due_date")) or now

        # Submission timing
        is_late = rng.random() < float(profile["late_prob"])
        if is_late:
            late_minutes = rng.randint(15, 60 * 36)
            submitted_at = due_date + timedelta(minutes=late_minutes)
            if submitted_at > now:
                submitted_at = now - timedelta(minutes=rng.randint(0, 60))
                is_late = submitted_at > due_date
        else:
            early_lo, early_hi = profile["early_minutes_range"]
            submitted_at = due_date - timedelta(
                minutes=rng.randint(int(early_lo), int(max(early_lo, early_hi)))
            )
            if submitted_at > now:
                submitted_at = now - timedelta(minutes=rng.randint(0, 60))
            is_late = submitted_at > due_date

        elapsed_time = None
        if assignment.get("duration_enabled") and assignment.get("duration"):
            duration_min = float(assignment["duration"])
            pct_lo, pct_hi = profile["elapsed_pct_range"]
            elapsed_time = max(60, int(duration_min * 60 * rng.uniform(pct_lo, pct_hi)))

        # Score derivation
        answer_payloads: list[dict[str, Any]] = []
        if questions:
            per_q_max = _per_question_max(total_score, len(questions))
            earned_total = 0.0
            for q in questions:
                qid = int(q["id"])
                qtype = (q.get("type") or "").lower()
                if qtype == "mcq":
                    _grade_mcq_question.threshold = float(profile["mcq_correct_prob"])  # type: ignore[attr-defined]
                    is_correct, answer_content = _grade_mcq_question(q, rng)
                    earned = per_q_max if is_correct else 0.0
                    answer_payloads.append(
                        {
                            "submission_id": None,
                            "question_id": qid,
                            "answer_content": answer_content,
                            "is_correct": bool(is_correct),
                            "earned_score": round(earned, 2),
                            "ai_feedback": None,
                        }
                    )
                else:
                    pct = rng.uniform(*profile["score_pct_range"])
                    earned = max(0.0, min(per_q_max, per_q_max * pct))
                    answer_payloads.append(
                        {
                            "submission_id": None,
                            "question_id": qid,
                            "answer_content": _placeholder_essay(q, rng),
                            "is_correct": None,
                            "earned_score": round(earned, 2),
                            "ai_feedback": None,
                        }
                    )
                earned_total += answer_payloads[-1]["earned_score"]
            final_score = (
                round(min(earned_total, total_score), 2) if total_score else round(earned_total, 2)
            )
        else:
            # Manual assignment (no questions). Score directly from persona band.
            pct = rng.uniform(*profile["score_pct_range"])
            final_score = round(total_score * pct, 2) if total_score else 0.0

        sub_payload = {
            "student_id": user_id,
            "assignment_id": aid,
            "status": "submitted",
            "is_corrected": True,
            "final_score": final_score,
            "submitted_at": submitted_at.isoformat(),
            "is_late": bool(is_late),
            "elapsed_time": elapsed_time,
            "feedback": _short_feedback(persona_name, final_score, total_score, rng),
            "risk_score": None,
        }
        inserted_row = (
            sb.table("assignment_submissions").insert(sub_payload).execute().data or []
        )
        if not inserted_row:
            continue
        submission_id = int(inserted_row[0]["id"])
        if answer_payloads:
            for a in answer_payloads:
                a["submission_id"] = submission_id
            sb.table("assignment_submission_answers").insert(answer_payloads).execute()
        inserted += 1

    if log_prefix:
        print(f"{log_prefix}[ok ] submissions: +{inserted} new (already had {len(already_submitted)})")
    return inserted


def _placeholder_essay(question: dict[str, Any], rng) -> str:
    seeds = [
        "Discussed the main concepts covered in lecture, with examples from the assigned reading.",
        "Compared the trade-offs between the two approaches and gave a recommendation backed by the lab data.",
        "Walked through the algorithm step by step and analysed its time/space complexity.",
        "Summarised the key takeaways and proposed an extension based on the case study.",
        "Outlined the architecture, justified the design choices, and noted limitations to revisit.",
    ]
    return rng.choice(seeds)


def _short_feedback(persona: str, final_score: float, total_score: float, rng) -> str | None:
    if total_score <= 0:
        return None
    pct = final_score / total_score
    pool = {
        "high": [
            "Solid work — clear reasoning, well-supported.",
            "Excellent grasp of the material; keep this rhythm.",
        ],
        "mid": [
            "On track. Tighten the explanation in the second half.",
            "Reasonable answer; revisit the corner cases for next time.",
        ],
        "low": [
            "Some gaps here — review the lecture and stop by office hours.",
            "Below the bar. Use the rubric to identify what to fix.",
        ],
    }
    band = "high" if pct >= 0.8 else "mid" if pct >= 0.5 else "low"
    if persona == "at_risk" and band == "low":
        return rng.choice(pool["low"])
    if rng.random() < 0.6:
        return rng.choice(pool[band])
    return None


def _seed_cli(args: argparse.Namespace) -> int:
    sb = get_supabase(service_role=True)
    if sb is None:
        raise SystemExit("Supabase service-role client not configured.")
    user = _resolve_student(sb, args.email)
    user_id = str(user["user_id"])
    courses = _resolve_courses(sb, user_id)
    if not courses:
        raise SystemExit(f"User {args.email} has no course_enrollments rows.")
    print(f"Seeding submissions for {user.get('full_name') or args.email} ({user_id})")
    inserted = seed_submissions_for_student(
        sb, user_id=user_id, courses=courses, persona_name=args.persona, log_prefix="  ",
    )
    print(f"\nInserted {inserted} graded submission(s).")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed graded assignment submissions for one student.")
    p.add_argument("--email", required=True)
    p.add_argument("--persona", choices=sorted(PERSONAS.keys()), default="steady")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(asyncio.to_thread(_seed_cli, args)))


if __name__ == "__main__":
    main()
