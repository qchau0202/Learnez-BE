"""Conversational intake that builds the student's learning path.

This module powers the "AI agent" the student talks to when they click
*Build my learning path*. The agent is **not** the general chatbot — it
is a small, deterministic state machine that:

1. Asks 3–5 fixed questions about the student's interests, math comfort,
   target career and elective bandwidth.
2. Combines the answers with the student's real enrollment history
   (completed + in-progress courses from ``public.course_enrollments``)
   and the curriculum catalog to compute a *preview* learning plan.
3. Waits for the student to **explicitly confirm** before persisting
   anything. Until confirmation the intake session lives in a TTL-bound
   Mongo collection (``learnez_ai.learning_path_intake_sessions``) and
   the active learning path remains untouched.

The questions are intentionally a fixed bank rather than an LLM-driven
free-form dialogue:

* It keeps the demo deterministic — important for screen-recordings and
  reproducible analytics on which tracks students pick.
* It avoids burning OpenAI/etc. credits on every intake.
* It guarantees the agent collects every signal the path generator
  actually consumes (math comfort, career direction, …).

If you want to layer an LLM on top later, the right insertion point is
:func:`compose_preview`'s output — feed the generated plan plus the raw
answers into a model for a polished prose summary.

Document shapes
---------------

``learning_path_intake_sessions`` (TTL, 24h):

.. code-block:: json

    {
        "_id":           "<uuid>",
        "user_id":       "<supabase user uuid>",
        "status":        "asking" | "ready" | "confirmed" | "cancelled",
        "asked_at":      ISODate(...),
        "answers":       { "<question_id>": ["<option_id>", ...], ... },
        "next_question": "<question_id>" | null,
        "preview":       <SavedLearningPath doc or null>,
        "created_at":    ISODate(...),
        "expires_at":    ISODate(...)
    }

``learning_paths`` (after confirm; one active per student):

.. code-block:: json

    {
        "_id":          ObjectId(...),
        "user_id":      "<supabase user uuid>",
        "status":       "active" | "archived",
        "path_version": "v1",
        "intake_id":    "<uuid>",
        "answers":      { ... },
        "header":       { ... },
        "courses":      [ <plan course>, ... ],
        "generated_at": ISODate(...),
        "confirmed_at": ISODate(...)
    }
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.core.database import get_mongo_ai_db, get_supabase

logger = logging.getLogger(__name__)

INTAKE_COLLECTION = "learning_path_intake_sessions"
SAVED_PATHS_COLLECTION = "learning_paths"
INTAKE_TTL_HOURS = 24


# --------------------------------------------------------------------------- #
# Question bank
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class IntakeOption:
    id: str
    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class IntakeQuestion:
    id: str
    prompt: str
    subtitle: str
    input_type: str  # "single_choice" | "multi_choice"
    options: tuple[IntakeOption, ...]
    min_select: int = 1
    max_select: int = 1


# Order matters: ``QUESTION_BANK`` is the canonical sequence served to the
# UI. Keep IDs stable — Mongo documents and analytics joins key off them.
QUESTION_BANK: tuple[IntakeQuestion, ...] = (
    IntakeQuestion(
        id="track",
        prompt="Which area of software are you most drawn to right now?",
        subtitle=(
            "Pick the track that sparks the most curiosity. You can change "
            "direction later — this just helps the path lean in a sensible "
            "direction for your next couple of semesters."
        ),
        input_type="single_choice",
        options=(
            IntakeOption("web", "Web development", "Front-end, back-end, full-stack apps."),
            IntakeOption("mobile", "Mobile apps", "iOS, Android, cross-platform."),
            IntakeOption("ai_ml", "AI / Machine Learning", "Models, recommendations, NLP, CV."),
            IntakeOption("data", "Data engineering", "Pipelines, warehousing, analytics."),
            IntakeOption("security", "Cybersecurity", "AppSec, network, cloud, IoT."),
            IntakeOption("game", "Game development", "Engines, graphics, gameplay."),
            IntakeOption("cloud", "Cloud / DevOps", "Distributed systems, deployment, SRE."),
        ),
    ),
    IntakeQuestion(
        id="math_comfort",
        prompt="How comfortable are you with maths-heavy courses?",
        subtitle=(
            "Honest answers help — we use this to decide whether to include "
            "courses like Calculus, Linear Algebra, and ML deep dives."
        ),
        input_type="single_choice",
        options=(
            IntakeOption("strong", "Strong — I enjoy maths"),
            IntakeOption("comfortable", "Comfortable — I can keep up"),
            IntakeOption("learning", "Still learning the basics"),
            IntakeOption("avoid", "I'd rather avoid the heaviest maths"),
        ),
    ),
    IntakeQuestion(
        id="career",
        prompt="What do you want your first step after graduation to look like?",
        subtitle=(
            "We'll weight project / internship / research courses differently "
            "depending on the goal."
        ),
        input_type="single_choice",
        options=(
            IntakeOption("industry", "Industry job", "Land a developer / engineer role."),
            IntakeOption("startup", "Build my own thing", "Found or join an early-stage team."),
            IntakeOption("grad_school", "Higher study", "Master's / research."),
            IntakeOption("undecided", "Undecided — keep options open"),
        ),
    ),
    IntakeQuestion(
        id="pace",
        prompt="How heavy do you want each upcoming semester to be?",
        subtitle=(
            "We'll match the number of recommended courses per term to your "
            "preferred pace."
        ),
        input_type="single_choice",
        options=(
            IntakeOption("light", "Light", "About 3 courses per semester."),
            IntakeOption("normal", "Normal", "About 4 courses per semester."),
            IntakeOption("heavy", "Heavy", "5 or more courses per semester."),
        ),
    ),
    IntakeQuestion(
        id="extras",
        prompt="Any side skills you'd like the path to include?",
        subtitle="Pick up to three — purely optional but helps personalise electives.",
        input_type="multi_choice",
        options=(
            IntakeOption("pm", "Project management"),
            IntakeOption("uiux", "UI/UX design"),
            IntakeOption("testing", "Software testing / QA"),
            IntakeOption("iot", "IoT / hardware"),
            IntakeOption("cloud", "Cloud / infrastructure"),
            IntakeOption("comm", "Communication / writing"),
        ),
        min_select=0,
        max_select=3,
    ),
)
QUESTION_BY_ID = {q.id: q for q in QUESTION_BANK}


class IntakeError(Exception):
    """Base class for intake errors surfaced to the API layer."""


class IntakeNotFound(IntakeError):
    """The intake session id is unknown / expired."""


class IntakeInvalidAnswer(IntakeError):
    """The submitted answer doesn't satisfy the question's constraints."""


class IntakeAlreadyConfirmed(IntakeError):
    """Tried to mutate a session that's already been confirmed."""


# --------------------------------------------------------------------------- #
# Track configuration — codes preferred per career / interest track.
# --------------------------------------------------------------------------- #


# Curated per-track "lean toward" lists. These are intentionally short —
# the generator picks from them first, then back-fills from the broader
# catalog if the student's preferred pace asks for more courses than the
# focused list contains.
TRACK_PREFERENCES: dict[str, tuple[str, ...]] = {
    "web": (
        "502070", "503073", "502093", "502094", "504070",
        "503108", "504077", "504087", "504091",
    ),
    "mobile": (
        "503074", "502071", "503108", "504077", "502093",
        "504087", "504091",
    ),
    "ai_ml": (
        "503043", "503044", "503080", "503117", "504048",
        "505043", "504105", "503040",
    ),
    "data": (
        "502051", "502097", "504048", "505043", "504105",
        "504049", "504087",
    ),
    "security": (
        "504088", "504093", "504101", "503103", "502049",
        "505063",
    ),
    "game": (
        "502067", "504076", "503111", "505065", "503108",
        "504077",
    ),
    "cloud": (
        "504087", "504093", "504070", "504091", "505063",
        "502094",
    ),
}

# Side-skill → extra course nudges.
EXTRA_PREFERENCES: dict[str, tuple[str, ...]] = {
    "pm": ("505009", "503109"),
    "uiux": ("503108",),
    "testing": ("502072", "504058"),
    "iot": ("502068", "503103", "505065"),
    "cloud": ("504087", "504093"),
    "comm": ("505010", "505012"),
}

MATH_HEAVY_CODES: frozenset[str] = frozenset({
    "501031", "501032", "501044", "502061", "503040",
})

CAREER_BOOSTS: dict[str, tuple[str, ...]] = {
    "industry": ("502090", "504074", "504091", "512CM6"),
    "startup": ("505009", "504091", "503108", "502093"),
    "grad_school": ("503117", "503080", "504105", "503040"),
    "undecided": ("502090", "504091", "505009"),
}

PACE_TO_COUNT: dict[str, int] = {
    "light": 6,
    "normal": 8,
    "heavy": 10,
}


# --------------------------------------------------------------------------- #
# Public dataclasses (mirrors the API response shape).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlannedCourse:
    course_id: int | None
    course_code: str
    title: str
    status: str  # "completed" | "in_progress" | "upcoming"
    semester: int | None
    academic_year: str | None
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "course_id": self.course_id,
            "course_code": self.course_code,
            "title": self.title,
            "status": self.status,
            "semester": self.semester,
            "academic_year": self.academic_year,
            "rationale": self.rationale,
        }


@dataclass
class IntakeSession:
    """In-memory view of an intake session as returned to the API layer."""

    session_id: str
    user_id: str
    status: str
    next_question: IntakeQuestion | None
    answers: dict[str, list[str]] = field(default_factory=dict)
    preview: list[PlannedCourse] | None = None
    preview_header: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "status": self.status,
            "next_question": _question_to_dict(self.next_question) if self.next_question else None,
            "answers": self.answers,
            "preview": (
                [c.to_dict() for c in self.preview] if self.preview is not None else None
            ),
            "preview_header": self.preview_header,
            "progress": _progress_for(self.answers),
            "total_questions": len(QUESTION_BANK),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def _question_to_dict(q: IntakeQuestion) -> dict[str, Any]:
    return {
        "id": q.id,
        "prompt": q.prompt,
        "subtitle": q.subtitle,
        "input_type": q.input_type,
        "min_select": q.min_select,
        "max_select": q.max_select,
        "options": [
            {"id": opt.id, "label": opt.label, "description": opt.description}
            for opt in q.options
        ],
    }


def _progress_for(answers: dict[str, list[str]]) -> int:
    answered = sum(1 for q in QUESTION_BANK if answers.get(q.id))
    return answered


def _next_question(answers: dict[str, list[str]]) -> IntakeQuestion | None:
    for q in QUESTION_BANK:
        if q.id not in answers:
            return q
    return None


# --------------------------------------------------------------------------- #
# Supabase helpers — pull the student's existing record so the preview can
# pin completed + in-progress courses.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _EnrolledCourse:
    id: int
    course_code: str | None
    title: str | None
    is_complete: bool
    semester: int | None
    academic_year: str | None
    course_start_date: str | None
    course_end_date: str | None


def _fetch_student_enrollments(user_id: str) -> list[_EnrolledCourse]:
    sb = get_supabase(service_role=True)
    if sb is None:
        return []
    enr_rows = (
        sb.table("course_enrollments")
        .select("course_id")
        .eq("student_id", user_id)
        .execute()
        .data
        or []
    )
    course_ids = sorted({int(r["course_id"]) for r in enr_rows if r.get("course_id") is not None})
    if not course_ids:
        return []
    out: list[_EnrolledCourse] = []
    # Chunk in case a student gets a giant enrollment list in the future.
    for i in range(0, len(course_ids), 200):
        chunk = (
            sb.table("courses")
            .select(
                "id, course_code, title, is_complete, semester, academic_year, "
                "course_start_date, course_end_date"
            )
            .in_("id", course_ids[i : i + 200])
            .execute()
            .data
            or []
        )
        for r in chunk:
            out.append(
                _EnrolledCourse(
                    id=int(r["id"]),
                    course_code=r.get("course_code"),
                    title=r.get("title"),
                    is_complete=bool(r.get("is_complete")),
                    semester=r.get("semester"),
                    academic_year=r.get("academic_year"),
                    course_start_date=r.get("course_start_date"),
                    course_end_date=r.get("course_end_date"),
                )
            )
    return out


def _fetch_catalog_courses(exclude_ids: set[int]) -> list[_EnrolledCourse]:
    sb = get_supabase(service_role=True)
    if sb is None:
        return []
    rows = (
        sb.table("courses")
        .select(
            "id, course_code, title, is_complete, semester, academic_year, "
            "course_start_date, course_end_date"
        )
        .eq("is_complete", False)
        .execute()
        .data
        or []
    )
    return [
        _EnrolledCourse(
            id=int(r["id"]),
            course_code=r.get("course_code"),
            title=r.get("title"),
            is_complete=bool(r.get("is_complete")),
            semester=r.get("semester"),
            academic_year=r.get("academic_year"),
            course_start_date=r.get("course_start_date"),
            course_end_date=r.get("course_end_date"),
        )
        for r in rows
        if r.get("id") is not None and int(r["id"]) not in exclude_ids
    ]


# --------------------------------------------------------------------------- #
# Preview generator
# --------------------------------------------------------------------------- #


def _classify_enrollment(course: _EnrolledCourse, today: datetime) -> str:
    """Bucket an existing enrollment into completed / in_progress / upcoming."""
    if course.is_complete:
        return "completed"
    end_iso = course.course_end_date
    if end_iso:
        try:
            end = datetime.fromisoformat(end_iso).replace(tzinfo=timezone.utc)
        except ValueError:
            end = None
        if end is not None and end < today:
            return "completed"
    start_iso = course.course_start_date
    if start_iso:
        try:
            start = datetime.fromisoformat(start_iso).replace(tzinfo=timezone.utc)
        except ValueError:
            start = None
        if start is not None and start > today:
            return "upcoming"
    return "in_progress"


def _rationale_for_existing(course: _EnrolledCourse, status: str) -> str:
    if status == "completed":
        return "Already completed — counts toward your GPA."
    if status == "in_progress":
        return "Currently enrolled — keep going."
    return "Upcoming on your registration."


def _select_upcoming_codes(
    *,
    answers: dict[str, list[str]],
    available_by_code: dict[str, _EnrolledCourse],
) -> list[str]:
    """Score each catalog course against the student's answers, return ordered codes."""
    track = (answers.get("track") or [""])[0]
    math = (answers.get("math_comfort") or [""])[0]
    career = (answers.get("career") or [""])[0]
    pace = (answers.get("pace") or ["normal"])[0]
    extras = answers.get("extras") or []

    target_count = PACE_TO_COUNT.get(pace, 8)
    preferred = list(TRACK_PREFERENCES.get(track, ()))
    boosts = set(CAREER_BOOSTS.get(career, ()))
    extra_preferred: list[str] = []
    for e in extras:
        extra_preferred.extend(EXTRA_PREFERENCES.get(e, ()))

    scores: dict[str, float] = {}
    for code, course in available_by_code.items():
        score = 0.0
        if code in preferred:
            # Earlier in the preference list → higher score.
            score += 5.0 - 0.1 * preferred.index(code)
        if code in boosts:
            score += 2.0
        if code in extra_preferred:
            # Multiple extras may reference the same code; count each.
            score += 1.0 * sum(1 for c in extra_preferred if c == code)
        if code in MATH_HEAVY_CODES and math in {"learning", "avoid"}:
            score -= 4.0 if math == "avoid" else 1.5
        if course.is_complete:
            # Should already be filtered out; double-check just in case.
            continue
        if score > 0:
            scores[code] = score

    # Stable sort by (-score, code) so re-runs with the same answers are
    # reproducible.
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    out: list[str] = [code for code, _ in ordered]
    # If we still don't have enough, top up with anything in the track
    # preference list (even if scored zero) and then anything from the
    # catalog — better to recommend something than show an empty path.
    for code in preferred:
        if code in available_by_code and code not in out:
            out.append(code)
    for code, course in available_by_code.items():
        if code in out:
            continue
        if course.is_complete:
            continue
        out.append(code)
    return out[:target_count]


def _rationale_for_upcoming(*, code: str, answers: dict[str, list[str]]) -> str:
    track = (answers.get("track") or [""])[0]
    career = (answers.get("career") or [""])[0]
    extras = answers.get("extras") or []
    bits: list[str] = []
    if track and code in TRACK_PREFERENCES.get(track, ()):
        bits.append(f"matches your {track.replace('_', '/')} focus")
    if career and code in CAREER_BOOSTS.get(career, ()):
        bits.append(f"helpful for {career.replace('_', ' ')}")
    for extra in extras:
        if code in EXTRA_PREFERENCES.get(extra, ()):
            bits.append(f"covers {extra}")
            break
    if not bits:
        bits.append("rounds out your remaining electives")
    return "Recommended because it " + ", and ".join(bits) + "."


def compose_preview(*, user_id: str, answers: dict[str, list[str]]) -> tuple[list[PlannedCourse], dict[str, Any]]:
    """Build the preview path *without persisting it*.

    Returns the ordered list of courses (completed → in-progress → upcoming)
    plus a header dict the API can surface verbatim. No Mongo writes happen
    here — callers must explicitly call :func:`confirm_intake` to save.
    """
    today = datetime.now(timezone.utc)
    existing = _fetch_student_enrollments(user_id)

    completed: list[PlannedCourse] = []
    in_progress: list[PlannedCourse] = []
    upcoming_from_existing: list[PlannedCourse] = []
    existing_ids: set[int] = set()

    for e in existing:
        existing_ids.add(e.id)
        status = _classify_enrollment(e, today)
        planned = PlannedCourse(
            course_id=e.id,
            course_code=e.course_code or "",
            title=e.title or "Untitled course",
            status=status,
            semester=e.semester,
            academic_year=e.academic_year,
            rationale=_rationale_for_existing(e, status),
        )
        if status == "completed":
            completed.append(planned)
        elif status == "in_progress":
            in_progress.append(planned)
        else:
            upcoming_from_existing.append(planned)

    catalog = _fetch_catalog_courses(existing_ids)
    available_by_code: dict[str, _EnrolledCourse] = {}
    for c in catalog:
        code = (c.course_code or "").strip()
        if code:
            available_by_code[code] = c

    chosen_codes = _select_upcoming_codes(answers=answers, available_by_code=available_by_code)
    upcoming: list[PlannedCourse] = []
    for code in chosen_codes:
        c = available_by_code[code]
        upcoming.append(
            PlannedCourse(
                course_id=c.id,
                course_code=code,
                title=c.title or "Untitled course",
                status="upcoming",
                semester=c.semester,
                academic_year=c.academic_year,
                rationale=_rationale_for_upcoming(code=code, answers=answers),
            )
        )

    plan = completed + in_progress + upcoming_from_existing + upcoming
    header = _compose_header(answers=answers, plan=plan, completed=completed)
    return plan, header


def _compose_header(
    *,
    answers: dict[str, list[str]],
    plan: list[PlannedCourse],
    completed: list[PlannedCourse],
) -> dict[str, Any]:
    track_label = _label_for("track", (answers.get("track") or [""])[0])
    career_label = _label_for("career", (answers.get("career") or [""])[0])
    pace_label = _label_for("pace", (answers.get("pace") or ["normal"])[0])
    bits: list[str] = []
    if track_label:
        bits.append(f"focus on {track_label.lower()}")
    if career_label and career_label.lower() != "undecided — keep options open":
        bits.append(career_label.lower())
    if pace_label:
        bits.append(f"a {pace_label.lower()} semester load")
    desc = "Your path is tuned to " + ", ".join(bits) if bits else "Personalised path"
    return {
        "name": f"{track_label} pathway" if track_label else "Personalised learning pathway",
        "description": desc + ".",
        "track": track_label,
        "completed_count": len(completed),
        "total_count": len(plan),
    }


def _label_for(question_id: str, option_id: str) -> str:
    q = QUESTION_BY_ID.get(question_id)
    if not q:
        return ""
    for opt in q.options:
        if opt.id == option_id:
            return opt.label
    return ""


# --------------------------------------------------------------------------- #
# Mongo persistence
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expiry() -> datetime:
    return _now() + timedelta(hours=INTAKE_TTL_HOURS)


def _session_from_doc(doc: dict[str, Any]) -> IntakeSession:
    answers = {k: list(v or []) for k, v in (doc.get("answers") or {}).items()}
    preview_dicts = doc.get("preview") or None
    preview: list[PlannedCourse] | None = None
    if preview_dicts:
        preview = [
            PlannedCourse(
                course_id=p.get("course_id"),
                course_code=p.get("course_code") or "",
                title=p.get("title") or "",
                status=p.get("status") or "upcoming",
                semester=p.get("semester"),
                academic_year=p.get("academic_year"),
                rationale=p.get("rationale") or "",
            )
            for p in preview_dicts
        ]
    next_q_id = doc.get("next_question")
    next_q = QUESTION_BY_ID.get(next_q_id) if next_q_id else None
    return IntakeSession(
        session_id=str(doc.get("_id")),
        user_id=str(doc.get("user_id") or ""),
        status=str(doc.get("status") or "asking"),
        next_question=next_q,
        answers=answers,
        preview=preview,
        preview_header=doc.get("preview_header"),
        created_at=doc.get("created_at") or _now(),
        expires_at=doc.get("expires_at"),
    )


async def start_intake(*, user_id: str) -> IntakeSession:
    """Open a fresh intake session and return its first question.

    A student can have multiple in-flight intakes (e.g. they started on
    one tab, then opened another). The most recent ``asking`` session
    wins on confirm — older ones expire silently via the TTL index.
    """
    db = get_mongo_ai_db()
    session_id = str(uuid.uuid4())
    now = _now()
    first_q = _next_question({})
    doc = {
        "_id": session_id,
        "user_id": user_id,
        "status": "asking",
        "answers": {},
        "next_question": first_q.id if first_q else None,
        "preview": None,
        "preview_header": None,
        "created_at": now,
        "expires_at": _expiry(),
        "asked_at": now,
    }
    await db[INTAKE_COLLECTION].insert_one(doc)
    return _session_from_doc(doc)


async def submit_answer(
    *,
    user_id: str,
    session_id: str,
    question_id: str,
    selected: list[str],
) -> IntakeSession:
    """Persist one answer and either advance to the next question or build the preview.

    Validates ``selected`` against the question's option ids and select
    bounds before writing. When the last question is answered the
    function transitions the session to ``ready`` and stores the
    generated preview so a follow-up GET can render it without recomputing.
    """
    db = get_mongo_ai_db()
    doc = await db[INTAKE_COLLECTION].find_one({"_id": session_id, "user_id": user_id})
    if doc is None:
        raise IntakeNotFound(f"Intake session {session_id} not found.")
    if doc.get("status") == "confirmed":
        raise IntakeAlreadyConfirmed("This intake has already been confirmed.")
    if doc.get("status") == "cancelled":
        raise IntakeNotFound("This intake was cancelled.")

    q = QUESTION_BY_ID.get(question_id)
    if q is None:
        raise IntakeInvalidAnswer(f"Unknown question id: {question_id}")

    valid_option_ids = {opt.id for opt in q.options}
    cleaned = [s for s in selected if s in valid_option_ids]
    if len(cleaned) < q.min_select:
        raise IntakeInvalidAnswer(
            f"Question '{q.id}' requires at least {q.min_select} option(s)."
        )
    if len(cleaned) > q.max_select:
        raise IntakeInvalidAnswer(
            f"Question '{q.id}' accepts at most {q.max_select} option(s)."
        )
    if q.input_type == "single_choice" and len(cleaned) != 1:
        raise IntakeInvalidAnswer(
            f"Question '{q.id}' is single-choice — pick exactly one option."
        )

    answers = {k: list(v or []) for k, v in (doc.get("answers") or {}).items()}
    answers[question_id] = cleaned

    next_q = _next_question(answers)
    update: dict[str, Any] = {
        "answers": answers,
        "next_question": next_q.id if next_q else None,
        "expires_at": _expiry(),
        "asked_at": _now(),
    }
    if next_q is None:
        # All questions answered — synthesize the preview *now* so the
        # client sees it on the same round-trip without an extra call.
        preview, header = compose_preview(user_id=user_id, answers=answers)
        update["preview"] = [c.to_dict() for c in preview]
        update["preview_header"] = header
        update["status"] = "ready"

    await db[INTAKE_COLLECTION].update_one(
        {"_id": session_id, "user_id": user_id},
        {"$set": update},
    )
    doc.update(update)
    return _session_from_doc(doc)


async def get_session(*, user_id: str, session_id: str) -> IntakeSession:
    db = get_mongo_ai_db()
    doc = await db[INTAKE_COLLECTION].find_one({"_id": session_id, "user_id": user_id})
    if doc is None:
        raise IntakeNotFound(f"Intake session {session_id} not found.")
    return _session_from_doc(doc)


async def latest_active_session(*, user_id: str) -> IntakeSession | None:
    """Return the most recent non-final session for this student, if any.

    Used by the GET ``/learning-path`` endpoint to render the wizard mid-flow
    if the student reloads the page during intake.
    """
    db = get_mongo_ai_db()
    doc = await db[INTAKE_COLLECTION].find_one(
        {"user_id": user_id, "status": {"$in": ["asking", "ready"]}},
        sort=[("created_at", -1)],
    )
    if doc is None:
        return None
    return _session_from_doc(doc)


async def cancel_intake(*, user_id: str, session_id: str) -> None:
    db = get_mongo_ai_db()
    res = await db[INTAKE_COLLECTION].update_one(
        {"_id": session_id, "user_id": user_id, "status": {"$ne": "confirmed"}},
        {"$set": {"status": "cancelled", "expires_at": _expiry()}},
    )
    if res.matched_count == 0:
        raise IntakeNotFound(f"Intake session {session_id} not found.")


async def confirm_intake(*, user_id: str, session_id: str) -> dict[str, Any]:
    """Persist the preview to ``learning_paths`` and mark the session confirmed.

    Re-confirming the same session is a no-op (idempotent). Confirming a
    new session for the same user *archives* the previous active path —
    we never silently overwrite, so the analytics history stays intact.

    Returns the saved-path document (Mongo-shaped, not API-shaped — the
    API layer can map it as needed).
    """
    db = get_mongo_ai_db()
    doc = await db[INTAKE_COLLECTION].find_one({"_id": session_id, "user_id": user_id})
    if doc is None:
        raise IntakeNotFound(f"Intake session {session_id} not found.")
    if doc.get("status") == "cancelled":
        raise IntakeNotFound("This intake was cancelled.")

    if doc.get("status") != "ready":
        # Re-synthesize in case the session was somehow created without a
        # preview (e.g. a future code path that skips intermediate writes).
        answers = doc.get("answers") or {}
        preview, header = compose_preview(user_id=user_id, answers=answers)
        doc["preview"] = [c.to_dict() for c in preview]
        doc["preview_header"] = header

    now = _now()
    # Archive any previously-active path for this user. We keep the row
    # in Mongo with ``status="archived"`` so we still have a history.
    await db[SAVED_PATHS_COLLECTION].update_many(
        {"user_id": user_id, "status": "active"},
        {"$set": {"status": "archived", "archived_at": now}},
    )

    saved = {
        "user_id": user_id,
        "status": "active",
        "path_version": "v1",
        "intake_id": session_id,
        "answers": doc.get("answers") or {},
        "header": doc.get("preview_header") or {},
        "courses": doc.get("preview") or [],
        "generated_at": now,
        "confirmed_at": now,
        "updated_at": now,
    }
    result = await db[SAVED_PATHS_COLLECTION].insert_one(saved)
    saved["_id"] = str(result.inserted_id)

    await db[INTAKE_COLLECTION].update_one(
        {"_id": session_id, "user_id": user_id},
        {
            "$set": {
                "status": "confirmed",
                "confirmed_at": now,
                "saved_path_id": saved["_id"],
            }
        },
    )
    return saved


async def get_active_saved_path(*, user_id: str) -> dict[str, Any] | None:
    """Read the currently-active saved learning path for a student, or ``None``."""
    db = get_mongo_ai_db()
    doc = await db[SAVED_PATHS_COLLECTION].find_one(
        {"user_id": user_id, "status": "active"},
        sort=[("confirmed_at", -1)],
    )
    if doc is None:
        return None
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc


async def clear_user_path(*, user_id: str) -> int:
    """Hard-delete every saved path for one user — only used by admin tools.

    Returns the number of documents removed.
    """
    db = get_mongo_ai_db()
    res = await db[SAVED_PATHS_COLLECTION].delete_many({"user_id": user_id})
    return int(res.deleted_count or 0)


__all__ = [
    "QUESTION_BANK",
    "INTAKE_COLLECTION",
    "SAVED_PATHS_COLLECTION",
    "IntakeError",
    "IntakeNotFound",
    "IntakeInvalidAnswer",
    "IntakeAlreadyConfirmed",
    "IntakeSession",
    "PlannedCourse",
    "start_intake",
    "submit_answer",
    "get_session",
    "latest_active_session",
    "cancel_intake",
    "confirm_intake",
    "get_active_saved_path",
    "clear_user_path",
    "compose_preview",
]
