"""Analytics - Competency, Learning Path, Dropout Risk.

This module intentionally keeps scoring logic transparent so lecturers can inspect
why a student got a given recommendation or risk level.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from statistics import mean
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from joblib import load as joblib_load
from pydantic import BaseModel, Field

from app.api.deps import AiDbDep
from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP
from app.core.supabase_cache import DYNAMIC_CACHE, STATIC_CACHE, is_miss
from app.services.notifications.scenario_notifications import parse_iso_datetime
from ml.training.dataset_builder import FEATURE_COLUMNS
from ml.training.risk_bands import load_thresholds, risk_level_from_score, score_from_probabilities

router = APIRouter(prefix="/analytics", tags=["AI Analytics"])

_MODEL_PATH = Path(__file__).resolve().parents[3] / "ml" / "models" / "dropout_rf_composite.joblib"
_THRESHOLDS_PATH = Path(__file__).resolve().parents[3] / "ml" / "models" / "dropout_thresholds_composite.json"


class CompetencyDimension(BaseModel):
    name: str
    score: float = Field(ge=0.0, le=1.0)
    note: str


class CompetencyResponse(BaseModel):
    student_id: str
    weeks_used: int
    dimensions: list[CompetencyDimension]
    strengths: list[str]
    weaknesses: list[str]
    summary: str


class LearningPathResponse(BaseModel):
    student_id: str
    weeks_used: int
    focus_areas: list[str]
    recommended_actions: list[str]
    rationale: str


class DropoutRiskResponse(BaseModel):
    student_id: str
    week_start: datetime | None = None
    risk_level: str
    risk_score: float = Field(ge=0.0, le=1.0)
    top_factors: list[dict[str, Any]]
    model_path: str
    data_source: str


class StudentRiskCard(BaseModel):
    student_id: str
    course_id: int | None = None
    course_code: str | None = None
    course_title: str | None = None
    class_room: str | None = None
    week_start: datetime | None = None
    risk_level: str
    risk_score: float = Field(ge=0.0, le=1.0)
    avg_score_30d: float = Field(
        description="Average graded score on the 0-100 scale (rescaled from 0-10 LMS source)."
    )
    avg_grade_10: float = Field(
        default=0.0,
        description="Same metric on the 0-10 scale used by the UI grade column.",
    )
    attendance_rate: float
    inactivity_streak_days: int
    submissions_total: int = 0
    submissions_late: int = 0
    active_minutes: float = 0.0
    logins: int = 0
    summary: str
    student_code: str | None = None
    full_name: str | None = None
    email: str | None = None
    student_class: str | None = None
    faculty_id: int | None = None
    faculty_name: str | None = None
    department_id: int | None = None
    department_name: str | None = None


class RiskDriver(BaseModel):
    """Plain-language reason that explains why students are flagged as at-risk."""

    key: str
    label: str
    explanation: str
    students_affected: int
    pct_of_considered: float


class AnalyticsOverviewResponse(BaseModel):
    students_considered: int
    risk_distribution: dict[str, int]
    avg_risk_score: float
    avg_attendance_rate: float
    avg_score_30d: float
    avg_grade_10: float = 0.0
    risk_drivers: list[RiskDriver] = Field(default_factory=list)
    generated_at_utc: datetime
    data_source: str


class FacultyRiskBreakdown(BaseModel):
    faculty_id: int | None
    faculty_name: str | None
    students_considered: int
    risk_distribution: dict[str, int]
    avg_risk_score: float
    avg_attendance_rate: float
    avg_score_30d: float


class DepartmentRiskBreakdown(BaseModel):
    department_id: int | None
    department_name: str | None
    faculty_id: int | None
    faculty_name: str | None
    students_considered: int
    risk_distribution: dict[str, int]
    avg_risk_score: float
    avg_attendance_rate: float
    avg_score_30d: float


class HierarchyRiskOverview(BaseModel):
    students_considered: int
    risk_distribution: dict[str, int]
    avg_risk_score: float
    avg_attendance_rate: float
    avg_score_30d: float
    by_faculty: list[FacultyRiskBreakdown]
    by_department: list[DepartmentRiskBreakdown]
    generated_at_utc: datetime
    data_source: str


class StudentOverviewStat(BaseModel):
    """One headline KPI shown on the student analytics overview tab."""

    key: str
    label: str
    value: str
    raw_value: float
    change_pct: float | None = None
    direction: str  # "up" | "down" | "flat"
    insight: str


class StudentEngagementPoint(BaseModel):
    week_start: datetime
    label: str
    hours: float


class StudentGradeBucket(BaseModel):
    score: int = Field(ge=0, le=10)
    count: int = Field(ge=0)


class StudentCourseSummary(BaseModel):
    id: int
    course_code: str | None = None
    title: str
    semester: int | None = None
    academic_year: str | None = None
    class_room: str | None = None


class StudentOverviewResponse(BaseModel):
    student_id: str
    stats: list[StudentOverviewStat]
    engagement_series: list[StudentEngagementPoint]
    grade_distribution: list[StudentGradeBucket]
    courses: list[StudentCourseSummary]
    weeks_used: int
    generated_at_utc: datetime
    data_source: str


# --------------------------------------------------------------------------- #
# Student behavior tab models
# --------------------------------------------------------------------------- #


class StudentBehaviorStat(BaseModel):
    """One card in the "Avg Login Time / Weekly Hours / Avg Session" trio."""
    key: str
    label: str
    value: str
    raw_value: float | None = None
    insight: str


class StudentBehaviorHeatmapRow(BaseModel):
    """One row of the 24x7 behavior heatmap (minutes per cell)."""
    hour: str
    mon: int = Field(ge=0)
    tue: int = Field(ge=0)
    wed: int = Field(ge=0)
    thu: int = Field(ge=0)
    fri: int = Field(ge=0)
    sat: int = Field(ge=0)
    sun: int = Field(ge=0)


class StudentBehaviorCompetencyRow(BaseModel):
    """Per-course "competency snapshot" row (descriptive metrics)."""
    course_id: int
    course_code: str | None = None
    course_name: str
    avg_grade_10: float = Field(ge=0.0, le=10.0)
    completion_pct: int = Field(ge=0, le=100)
    attendance_pct: int = Field(ge=0, le=100)
    study_hours: int = Field(ge=0)


class StudentBehaviorGradedRow(BaseModel):
    """Per-course "graded work summary" row (latest weekly avg + delta)."""
    course_id: int
    course_code: str | None = None
    course_name: str
    latest_avg: float = Field(ge=0.0, le=100.0)
    delta_pts: int
    completion_pct: int = Field(ge=0, le=100)
    avg_grade_10: float = Field(ge=0.0, le=10.0)


class StudentBehaviorResponse(BaseModel):
    student_id: str
    week_start: datetime
    stats: list[StudentBehaviorStat]
    heatmap: list[StudentBehaviorHeatmapRow]
    competency: list[StudentBehaviorCompetencyRow]
    graded_summary: list[StudentBehaviorGradedRow]
    courses: list[StudentCourseSummary]
    weeks_used: int
    generated_at_utc: datetime
    data_source: str


# --------------------------------------------------------------------------- #
# Student Risk Analysis tab models
# --------------------------------------------------------------------------- #


class StudentRiskFactor(BaseModel):
    """One row in the "Dropout Risk Assessment" factor list."""
    name: str
    value: int = Field(ge=0, le=100)
    weight: str  # "positive" | "neutral" | "negative"


class StudentRiskBreakdownSlice(BaseModel):
    """One slice of the "Risk breakdown" donut chart."""
    name: str  # "Safe" | "At Risk"
    value: int = Field(ge=0, le=100)


class StudentRiskHighlight(BaseModel):
    """Either the strongest positive factor or the one to monitor."""
    name: str
    value: int = Field(ge=0, le=100)


class StudentRiskAnalysisResponse(BaseModel):
    student_id: str
    week_start: datetime | None = None
    risk_score_pct: int = Field(ge=0, le=100)
    risk_level: str
    factors: list[StudentRiskFactor]
    breakdown: list[StudentRiskBreakdownSlice]
    top_strength: StudentRiskHighlight | None = None
    monitor: StudentRiskHighlight | None = None
    headline: str
    courses: list[StudentCourseSummary]
    generated_at_utc: datetime
    data_source: str


# --------------------------------------------------------------------------- #
# Student Learning Path tab models
# --------------------------------------------------------------------------- #


class StudentPathAlternative(BaseModel):
    """Catalog course offered as a swap option for a path slot."""
    course_id: int | None = None
    code: str
    name: str
    reason: str


class StudentPathRecommendationCourse(BaseModel):
    """Inline catalog suggestion attached to a remedial / accelerated nudge."""
    course_id: int | None = None
    code: str
    name: str


class StudentPathRecommendation(BaseModel):
    """Performance-driven nudge attached to one path entry.

    ``kind`` is the dynamic recommendation flavour:

    * ``remedial`` — recent grade dipped; offer follow-up review courses.
    * ``accelerated`` — strong performance; offer next-step / advanced.
    * ``info`` — neutral coaching note (e.g., on track).
    """

    kind: str  # "remedial" | "accelerated" | "info"
    message: str
    suggestions: list[StudentPathRecommendationCourse] = Field(default_factory=list)


class StudentPathCourse(BaseModel):
    """One row in the personalised learning path.

    ``status`` is one of:
    * ``completed`` — graded and finished
    * ``in_progress`` — currently running
    * ``upcoming`` — enrolled but not started yet
    """

    id: str
    course_id: int | None = None
    code: str
    name: str
    status: str
    semester: int | None = None
    academic_year: str | None = None
    alternatives: list[StudentPathAlternative]
    recommendation: StudentPathRecommendation | None = None


class StudentPathNextStep(BaseModel):
    """Catalog course recommended as a *future* step (not yet enrolled).

    Surfaced in a dedicated "Recommended next" section instead of being
    mixed into the personal path — the student decides whether to enroll.
    """

    course_id: int
    code: str
    name: str
    semester: int | None = None
    academic_year: str | None = None
    reason: str


class StudentPathHeader(BaseModel):
    """Header card shown above the reorderable course list."""
    name: str
    description: str
    major: str | None = None
    progress: int = Field(ge=0, le=100)


class StudentLearningPathResponse(BaseModel):
    student_id: str
    path: StudentPathHeader
    courses: list[StudentPathCourse]
    next_steps: list[StudentPathNextStep] = Field(default_factory=list)
    enrolled_courses: list[StudentCourseSummary]
    generated_at_utc: datetime
    data_source: str


@lru_cache(maxsize=1)
def _load_model():
    return joblib_load(_MODEL_PATH)


async def _load_weekly_feature_rows(db: AiDbDep, student_id: str, *, since_weeks: int) -> list[dict[str, Any]]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(weeks=since_weeks)
    cursor = (
        db["student_weekly_features"]
        .find({"user_id": student_id, "week_start": {"$gte": start, "$lt": end}})
        .sort([("week_start", -1)])
    )
    return await cursor.to_list(length=since_weeks)


def _authorized_course_scope(user: dict[str, Any]) -> tuple[set[int] | None, str]:
    """Return (allowed_course_ids_or_none_for_all, role_name)."""
    role_name = ROLE_MAP.get(user.get("role_id"))
    if not role_name:
        role_raw = str(user.get("role") or "").strip().lower()
        role_map = {"admin": "Admin", "lecturer": "Lecturer", "student": "Student"}
        role_name = role_map.get(role_raw)
    if role_name == "Admin":
        return None, role_name
    supabase = get_supabase(service_role=True)
    if not supabase:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")
    uid = user.get("user_id")
    if role_name == "Lecturer":
        rows = supabase.table("courses").select("id").eq("lecturer_id", uid).execute().data or []
        return {int(r["id"]) for r in rows if r.get("id") is not None}, role_name
    if role_name == "Student":
        rows = supabase.table("course_enrollments").select("course_id").eq("student_id", uid).execute().data or []
        return {int(r["course_id"]) for r in rows if r.get("course_id") is not None}, role_name
    raise HTTPException(status_code=403, detail="Forbidden")


async def _load_latest_rows_for_many_users(
    db: AiDbDep,
    *,
    since_weeks: int,
    max_users: int,
    allowed_course_ids: set[int] | None,
    course_id: int | None,
) -> list[dict[str, Any]]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(weeks=since_weeks)
    q: dict[str, Any] = {"week_start": {"$gte": start, "$lt": end}}
    if course_id is not None:
        q["course_id"] = course_id
    elif allowed_course_ids is not None:
        if not allowed_course_ids:
            return []
        q["course_id"] = {"$in": sorted(allowed_course_ids)}
    docs = await (
        db["student_weekly_features"]
        .find(q)
        .sort([("week_start", -1)])
        .to_list(length=max(max_users * 8, 1000))
    )
    latest_by_user_course: dict[tuple[str, int | None], dict[str, Any]] = {}
    for d in docs:
        uid = str(d.get("user_id") or "").strip()
        if uid:
            key = (uid, d.get("course_id"))
            if key in latest_by_user_course:
                continue
            latest_by_user_course[key] = d
            if len(latest_by_user_course) >= max_users:
                break
    return list(latest_by_user_course.values())


def _parse_datetime_maybe(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        dt = parse_iso_datetime(v)
        if dt is not None:
            return dt
    return None


def _risk_doc_to_card(doc: dict[str, Any]) -> StudentRiskCard | None:
    uid = str(doc.get("user_id") or doc.get("student_id") or "").strip()
    if not uid:
        return None
    raw_score = doc.get("risk_score", doc.get("score", 0.0))
    try:
        score = float(raw_score or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    raw_level = str(doc.get("risk_level") or "").strip().lower()
    if raw_level not in {"low", "medium", "high"}:
        low_max, med_max = load_thresholds(_THRESHOLDS_PATH)
        raw_level = risk_level_from_score(score, low_max, med_max)

    metrics = doc.get("metrics") or {}
    feats = doc.get("features") or {}
    attendance = metrics.get("attendance_rate", feats.get("attendance_rate", 0.0))
    avg_score = metrics.get("avg_score_30d", feats.get("avg_score_30d", 0.0))
    inactivity = metrics.get("inactivity_streak_days", feats.get("inactivity_streak_days", 0))
    subs_total = metrics.get("submissions_total", feats.get("submissions_total", 0))
    subs_late = metrics.get("submissions_late", feats.get("submissions_late", 0))
    active_min = metrics.get("active_minutes", feats.get("active_minutes", 0.0))
    logins = metrics.get("logins", feats.get("logins", 0))
    summary = (
        str(doc.get("summary") or doc.get("explanation") or doc.get("rationale") or "").strip()
        or _student_summary(feats if isinstance(feats, dict) else {}, raw_level)
    )
    week_start = (
        _parse_datetime_maybe(doc.get("week_start"))
        or _parse_datetime_maybe(doc.get("predicted_at"))
        or _parse_datetime_maybe(doc.get("created_at"))
    )
    return StudentRiskCard(
        student_id=uid,
        course_id=doc.get("course_id"),
        week_start=week_start,
        risk_level=raw_level,
        risk_score=round(score, 4),
        avg_score_30d=float(avg_score or 0.0),
        attendance_rate=float(attendance or 0.0),
        inactivity_streak_days=int(inactivity or 0),
        submissions_total=int(subs_total or 0),
        submissions_late=int(subs_late or 0),
        active_minutes=float(active_min or 0.0),
        logins=int(logins or 0),
        summary=summary,
    )


async def _load_risk_cards_from_risk_scores(
    db: AiDbDep,
    *,
    since_weeks: int,
    max_users: int,
    allowed_course_ids: set[int] | None,
    course_id: int | None,
) -> list[StudentRiskCard]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(weeks=since_weeks)
    q: dict[str, Any] = {
        "$or": [
            {"created_at": {"$gte": start, "$lt": end}},
            {"predicted_at": {"$gte": start, "$lt": end}},
            {"week_start": {"$gte": start, "$lt": end}},
        ]
    }
    if course_id is not None:
        q["course_id"] = course_id
    elif allowed_course_ids is not None:
        if not allowed_course_ids:
            return []
        q["course_id"] = {"$in": sorted(allowed_course_ids)}
    docs = await (
        db["risk_scores"]
        .find(q)
        .sort([("created_at", -1), ("predicted_at", -1), ("week_start", -1)])
        .to_list(length=max(max_users * 8, 1000))
    )
    latest_by_user_course: dict[tuple[str, int | None], StudentRiskCard] = {}
    for d in docs:
        card = _risk_doc_to_card(d)
        if card is None:
            continue
        key = (card.student_id, card.course_id)
        if key in latest_by_user_course:
            continue
        latest_by_user_course[key] = card
        if len(latest_by_user_course) >= max_users:
            break
    return list(latest_by_user_course.values())


def _load_enrollment_pairs_sync(allowed_course_ids: set[int] | None) -> set[tuple[str, int]] | None:
    """Sync core for :func:`_load_enrollment_pairs` — runs in a worker
    thread so the asyncio event loop isn't blocked while Supabase
    answers (which can take 100–500 ms per round-trip).

    Returns ``None`` when Supabase is unreachable so callers can fall back to
    not filtering rather than silently returning empty.
    """
    sb = get_supabase(service_role=True)
    if not sb:
        return None
    out: set[tuple[str, int]] = set()
    if allowed_course_ids is not None:
        if not allowed_course_ids:
            return out
        ids = sorted(int(x) for x in allowed_course_ids)
        for i in range(0, len(ids), 200):
            batch = ids[i : i + 200]
            rows = (
                sb.table("course_enrollments")
                .select("student_id, course_id")
                .in_("course_id", batch)
                .execute()
                .data
                or []
            )
            for r in rows:
                sid = str(r.get("student_id") or "").strip()
                cid = r.get("course_id")
                if sid and cid is not None:
                    out.add((sid, int(cid)))
        return out
    rows = sb.table("course_enrollments").select("student_id, course_id").execute().data or []
    for r in rows:
        sid = str(r.get("student_id") or "").strip()
        cid = r.get("course_id")
        if sid and cid is not None:
            out.add((sid, int(cid)))
    return out


async def _load_enrollment_pairs(
    allowed_course_ids: set[int] | None,
) -> set[tuple[str, int]] | None:
    """Async + 60s-TTL-cached variant of :func:`_load_enrollment_pairs_sync`.

    Cache key includes the course-id scope so an admin (no scope) and a
    lecturer (course-restricted) don't share buckets.
    """
    if allowed_course_ids is None:
        cache_key = "enrollment_pairs:all"
    else:
        # Sort so the same scope produces the same key regardless of
        # caller iteration order.
        cache_key = "enrollment_pairs:" + ",".join(
            str(c) for c in sorted(int(x) for x in allowed_course_ids)
        )
    cached = DYNAMIC_CACHE.get(cache_key)
    if not is_miss(cached):
        return cached
    result = await asyncio.to_thread(_load_enrollment_pairs_sync, allowed_course_ids)
    DYNAMIC_CACHE.set(cache_key, result)
    return result


def _load_course_meta_sync(course_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Sync core for :func:`_load_course_meta`. Runs in a worker thread."""
    if not course_ids:
        return {}
    sb = get_supabase(service_role=True)
    if not sb:
        return {}
    out: dict[int, dict[str, Any]] = {}
    ids = sorted({int(c) for c in course_ids})
    for i in range(0, len(ids), 200):
        batch = ids[i : i + 200]
        rows = (
            sb.table("courses")
            .select("id, course_code, title, class_room, from_department")
            .in_("id", batch)
            .execute()
            .data
            or []
        )
        for r in rows:
            cid = r.get("id")
            if cid is not None:
                out[int(cid)] = r
    return out


async def _load_course_meta(course_ids: list[int]) -> dict[int, dict[str, Any]]:
    """5-min-cached, off-thread course metadata lookup.

    Course rows rarely change at runtime so the cache TTL is generous.
    Cache key is the canonical sorted id list so two callers asking for
    the same set hit the same bucket.
    """
    if not course_ids:
        return {}
    ids = sorted({int(c) for c in course_ids})
    cache_key = "course_meta:" + ",".join(str(c) for c in ids)
    cached = STATIC_CACHE.get(cache_key)
    if not is_miss(cached):
        return cached
    result = await asyncio.to_thread(_load_course_meta_sync, ids)
    STATIC_CACHE.set(cache_key, result)
    return result


def _load_faculties_sync() -> dict[int, str]:
    """``id -> name`` for all faculties. Sync core, runs in a worker thread."""
    sb = get_supabase(service_role=True)
    if not sb:
        return {}
    fac_rows = sb.table("faculties").select("id, name").execute().data or []
    return {
        int(r["id"]): str(r.get("name") or "")
        for r in fac_rows
        if r.get("id") is not None
    }


def _load_departments_sync() -> dict[int, dict[str, Any]]:
    """``id -> {id, name, from_faculty}`` for all departments. Sync core."""
    sb = get_supabase(service_role=True)
    if not sb:
        return {}
    dep_rows = sb.table("departments").select("id, name, from_faculty").execute().data or []
    return {int(r["id"]): r for r in dep_rows if r.get("id") is not None}


async def _load_faculties_cached() -> dict[int, str]:
    cached = STATIC_CACHE.get("faculties")
    if not is_miss(cached):
        return cached
    result = await asyncio.to_thread(_load_faculties_sync)
    STATIC_CACHE.set("faculties", result)
    return result


async def _load_departments_cached() -> dict[int, dict[str, Any]]:
    cached = STATIC_CACHE.get("departments")
    if not is_miss(cached):
        return cached
    result = await asyncio.to_thread(_load_departments_sync)
    STATIC_CACHE.set("departments", result)
    return result


def _fetch_user_chunks_sync(
    table: str, columns: str, uids: list[str], chunk_size: int = 100
) -> list[dict[str, Any]]:
    """Chunked ``in.(...)`` fetch on a user-keyed table. Sync core.

    PostgREST encodes ``in.()`` filters in the URL; with several hundred
    UUIDs the URL exceeds the gateway limit and Supabase returns a plain
    ``Bad Request``. Chunk the lookups so each request stays small.
    """
    sb = get_supabase(service_role=True)
    if not sb:
        return []
    out_rows: list[dict[str, Any]] = []
    for i in range(0, len(uids), chunk_size):
        batch = uids[i : i + chunk_size]
        res = sb.table(table).select(columns).in_("user_id", batch).execute()
        out_rows.extend(res.data or [])
    return out_rows


async def _resolve_student_org_map(user_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Look up faculty/department/identity for a batch of student user_ids.

    Resolution rules:
      - Prefer ``student_profiles`` (department_id / faculty_id, plus student_id, class).
      - Faculty is resolved via ``departments.from_faculty`` when the profile's
        ``faculty_id`` is null but a department is set.
      - Falls back to ``users`` for full_name/email so admins can see real names.

    Optimisations layered on top of the original sync version:
      * Faculties + departments come from a 5-min static cache (one
        Supabase call per 5 min instead of every request).
      * The two chunked ``in.()`` lookups now run **concurrently** off
        the event loop — overlapping their network latency cuts the
        per-request cost roughly in half.
      * The whole resolved map is itself cached for 60 s keyed on the
        sorted user-id list.

    Returns a dict keyed by user_id.
    """
    if not user_ids:
        return {}
    uids = sorted({str(u).strip() for u in user_ids if str(u or "").strip()})
    if not uids:
        return {}

    # Cache by the cohort fingerprint. 60 s is short enough that admin
    # edits to a user profile show up quickly, long enough that the
    # dashboard's repeated polls hit the cache instead of Supabase.
    cache_key = "org_map:" + ",".join(uids)
    cached = DYNAMIC_CACHE.get(cache_key)
    if not is_miss(cached):
        return cached

    # Run the four independent lookups concurrently. ``faculties`` and
    # ``departments`` are usually warm-cached so they return instantly;
    # the chunked ``student_profiles`` and ``users`` queries dominate
    # and now overlap each other.
    faculty_name_by_id, dep_by_id, sp_rows, user_rows = await asyncio.gather(
        _load_faculties_cached(),
        _load_departments_cached(),
        asyncio.to_thread(
            _fetch_user_chunks_sync,
            "student_profiles",
            "user_id, student_id, class, faculty_id, department_id",
            uids,
        ),
        asyncio.to_thread(
            _fetch_user_chunks_sync,
            "users",
            "user_id, full_name, email",
            uids,
        ),
    )

    user_by_id = {str(r.get("user_id")): r for r in user_rows}
    out: dict[str, dict[str, Any]] = {}
    for sp in sp_rows:
        uid = str(sp.get("user_id") or "").strip()
        if not uid:
            continue
        dept_id = sp.get("department_id")
        fac_id = sp.get("faculty_id")
        dept_name = None
        if dept_id is not None:
            dept = dep_by_id.get(int(dept_id), {})
            dept_name = dept.get("name")
            if fac_id is None:
                fac_id = dept.get("from_faculty")
        fac_name = faculty_name_by_id.get(int(fac_id)) if fac_id is not None else None
        u = user_by_id.get(uid, {})
        out[uid] = {
            "student_code": sp.get("student_id"),
            "full_name": u.get("full_name"),
            "email": u.get("email"),
            "student_class": sp.get("class"),
            "faculty_id": int(fac_id) if fac_id is not None else None,
            "faculty_name": fac_name,
            "department_id": int(dept_id) if dept_id is not None else None,
            "department_name": dept_name,
        }
    # Backfill identity for any user_ids that have no student_profile row.
    for uid in uids:
        if uid in out:
            continue
        u = user_by_id.get(uid, {})
        out[uid] = {
            "student_code": None,
            "full_name": u.get("full_name"),
            "email": u.get("email"),
            "student_class": None,
            "faculty_id": None,
            "faculty_name": None,
            "department_id": None,
            "department_name": None,
        }
    DYNAMIC_CACHE.set(cache_key, out)
    return out


async def _annotate_cards_with_org(cards: list[StudentRiskCard]) -> list[StudentRiskCard]:
    """Hydrate each risk card with org / course metadata.

    Runs the org-map lookup and the course-meta lookup **concurrently**
    via :func:`asyncio.gather` — they hit different Supabase tables and
    have no inter-dependency, so overlapping their network round-trips
    cuts annotation latency roughly in half.
    """
    if not cards:
        return cards
    course_ids = sorted({int(c.course_id) for c in cards if isinstance(c.course_id, int)})
    org, course_meta = await asyncio.gather(
        _resolve_student_org_map([c.student_id for c in cards]),
        _load_course_meta(course_ids),
    )
    for c in cards:
        meta = org.get(c.student_id) or {}
        c.student_code = meta.get("student_code")
        c.full_name = meta.get("full_name")
        c.email = meta.get("email")
        c.student_class = meta.get("student_class")
        c.faculty_id = meta.get("faculty_id")
        c.faculty_name = meta.get("faculty_name")
        c.department_id = meta.get("department_id")
        c.department_name = meta.get("department_name")
        c.avg_grade_10 = round(float(c.avg_score_30d) / 10.0, 2)
        if isinstance(c.course_id, int):
            cm = course_meta.get(int(c.course_id)) or {}
            c.course_code = cm.get("course_code")
            c.course_title = cm.get("title")
            c.class_room = cm.get("class_room")
    return cards


def _filter_cards_to_real_students(
    cards: list[StudentRiskCard],
    *,
    enrollment_pairs: set[tuple[str, int]] | None,
) -> list[StudentRiskCard]:
    """Drop simulation-only cards.

    A card is "real" iff:
      - the student exists in Supabase ``users`` (we can tell because the
        annotation pass populated ``full_name`` or ``student_code``), AND
      - the ``(student_id, course_id)`` pair is present in
        ``course_enrollments`` when an enrollment set is available.

    If ``enrollment_pairs`` is ``None`` (Supabase unreachable) we fall back to
    the identity check only, so we never silently return an empty page.
    """
    out: list[StudentRiskCard] = []
    for c in cards:
        has_identity = bool(c.full_name or c.student_code or c.email)
        if not has_identity:
            continue
        if enrollment_pairs is not None:
            if c.course_id is None:
                continue
            if (c.student_id, int(c.course_id)) not in enrollment_pairs:
                continue
        out.append(c)
    return out


def _filter_cards_by_org(
    cards: list[StudentRiskCard],
    *,
    faculty_id: int | None,
    department_id: int | None,
) -> list[StudentRiskCard]:
    if faculty_id is None and department_id is None:
        return cards
    out: list[StudentRiskCard] = []
    for c in cards:
        if faculty_id is not None and (c.faculty_id or -1) != faculty_id:
            continue
        if department_id is not None and (c.department_id or -1) != department_id:
            continue
        out.append(c)
    return out


def _aggregate_distribution(cards: list[StudentRiskCard]) -> dict[str, Any]:
    risk_dist = {"low": 0, "medium": 0, "high": 0}
    scores: list[float] = []
    attendance_vals: list[float] = []
    grade_vals: list[float] = []
    for c in cards:
        risk_dist[c.risk_level] = risk_dist.get(c.risk_level, 0) + 1
        scores.append(float(c.risk_score))
        attendance_vals.append(float(c.attendance_rate))
        grade_vals.append(float(c.avg_score_30d))
    avg_score_100 = round(float(mean(grade_vals)), 4) if grade_vals else 0.0
    return {
        "students_considered": len(cards),
        "risk_distribution": risk_dist,
        "avg_risk_score": round(float(mean(scores)), 4) if scores else 0.0,
        "avg_attendance_rate": round(float(mean(attendance_vals)), 4) if attendance_vals else 0.0,
        "avg_score_30d": avg_score_100,
        "avg_grade_10": round(avg_score_100 / 10.0, 2),
    }


def _compute_risk_drivers(cards: list[StudentRiskCard]) -> list[RiskDriver]:
    """Plain-language reasons we surface to admins/lecturers.

    Each driver is a count + percentage of *students considered* who hit one of
    the simple, easily-explained risk thresholds. The thresholds are picked to
    line up with the heuristics in ``_student_friendly_reasons`` so the per-row
    "why" text stays consistent with the dashboard summary.
    """
    if not cards:
        return []
    n = len(cards)
    low_attendance = 0  # < 70%
    low_grade = 0  # < 5/10
    late_submissions = 0  # late > 50% of submissions, with at least 3 submissions
    long_inactivity = 0  # >= 7 days streak
    no_engagement = 0  # logins < 3 OR active_minutes < 45 in the latest week
    no_submissions = 0  # 0 submissions tracked

    for c in cards:
        if (c.attendance_rate or 0.0) < 0.70:
            low_attendance += 1
        if (c.avg_grade_10 or 0.0) > 0 and c.avg_grade_10 < 5.0:
            low_grade += 1
        if c.submissions_total >= 3 and c.submissions_late > c.submissions_total / 2:
            late_submissions += 1
        if c.inactivity_streak_days >= 7:
            long_inactivity += 1
        if c.logins < 3 or c.active_minutes < 45:
            no_engagement += 1
        if c.submissions_total == 0:
            no_submissions += 1

    def _pct(count: int) -> float:
        return round((count / n) * 100.0, 1) if n else 0.0

    return [
        RiskDriver(
            key="low_attendance",
            label="Attendance below 70%",
            explanation="Students whose attendance has dropped under 70% in the analysed window.",
            students_affected=low_attendance,
            pct_of_considered=_pct(low_attendance),
        ),
        RiskDriver(
            key="low_grade",
            label="Average grade below 5/10",
            explanation="Students whose recent average score is below the passing threshold.",
            students_affected=low_grade,
            pct_of_considered=_pct(low_grade),
        ),
        RiskDriver(
            key="frequent_late_submissions",
            label="Frequent late submissions",
            explanation="Students with at least 3 submissions where more than half were late.",
            students_affected=late_submissions,
            pct_of_considered=_pct(late_submissions),
        ),
        RiskDriver(
            key="long_inactivity",
            label="Inactive 7+ days",
            explanation="Students whose latest activity was more than a week ago.",
            students_affected=long_inactivity,
            pct_of_considered=_pct(long_inactivity),
        ),
        RiskDriver(
            key="low_engagement",
            label="Low platform engagement",
            explanation="Students with fewer than 3 logins or less than 45 active minutes recently.",
            students_affected=no_engagement,
            pct_of_considered=_pct(no_engagement),
        ),
        RiskDriver(
            key="no_submissions",
            label="No submissions yet",
            explanation="Students who have not submitted any assignment in the analysed window.",
            students_affected=no_submissions,
            pct_of_considered=_pct(no_submissions),
        ),
    ]


def _avg_feature(rows: list[dict[str, Any]], feature_name: str) -> float:
    vals: list[float] = []
    for row in rows:
        raw = (row.get("features") or {}).get(feature_name)
        if raw is None:
            continue
        vals.append(float(raw))
    return float(mean(vals)) if vals else 0.0


def _predict_risk_from_features(features: dict[str, Any]) -> tuple[float, str]:
    model = _load_model()
    vector = [[float(features.get(col) or 0.0) for col in FEATURE_COLUMNS]]
    proba_by_class: dict[int, float] = {}
    if hasattr(model, "predict_proba"):
        probs = model.predict_proba(vector)[0]
        classes = [int(x) for x in model.classes_.tolist()]
        proba_by_class = {classes[i]: float(probs[i]) for i in range(len(classes))}
    score = score_from_probabilities(proba_by_class)
    low_max, med_max = load_thresholds(_THRESHOLDS_PATH)
    level = risk_level_from_score(score, low_max, med_max)
    return score, level


def _student_summary(features: dict[str, Any], risk_level: str) -> str:
    avg_score = float(features.get("avg_score_30d") or 0.0)
    attendance = float(features.get("attendance_rate") or 0.0)
    inactivity = int(features.get("inactivity_streak_days") or 0)
    if risk_level == "high":
        return "High risk due to low academics/engagement trend; intervention recommended this week."
    if risk_level == "medium":
        return "Moderate risk; monitor attendance and submissions closely over next 1-2 weeks."
    if avg_score >= 75 and attendance >= 0.8 and inactivity <= 2:
        return "Low risk with stable learning habits and strong performance."
    return "Low risk currently, continue monitoring weekly behavior trend."


async def _load_student_weekly_rows(
    db: AiDbDep,
    *,
    student_id: str,
    since_weeks: int,
    course_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """All weekly feature rows for one student in a date window, optionally course-scoped."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(weeks=since_weeks)
    q: dict[str, Any] = {
        "user_id": student_id,
        "week_start": {"$gte": start, "$lt": end},
    }
    if course_ids is not None:
        if not course_ids:
            return []
        q["course_id"] = {"$in": sorted(course_ids)}
    cursor = db["student_weekly_features"].find(q).sort([("week_start", 1)])
    # Cap fetch at one row per course per week + buffer.
    return await cursor.to_list(length=since_weeks * 60)


def _student_enrolled_courses(student_id: str) -> list[dict[str, Any]]:
    """Resolve courses the student is enrolled in, with display metadata."""
    sb = get_supabase(service_role=True)
    if not sb:
        return []
    enr = (
        sb.table("course_enrollments")
        .select("course_id")
        .eq("student_id", student_id)
        .execute()
        .data
        or []
    )
    course_ids = sorted({int(r["course_id"]) for r in enr if r.get("course_id") is not None})
    if not course_ids:
        return []
    out: list[dict[str, Any]] = []
    for i in range(0, len(course_ids), 200):
        batch = course_ids[i : i + 200]
        rows = (
            sb.table("courses")
            .select("id, course_code, title, class_room, semester, academic_year")
            .in_("id", batch)
            .execute()
            .data
            or []
        )
        out.extend(rows)
    out.sort(key=lambda r: (str(r.get("academic_year") or ""), int(r.get("semester") or 0), str(r.get("course_code") or "")))
    return out


def _student_grade_buckets(
    student_id: str,
    *,
    course_ids: set[int] | None,
) -> list[StudentGradeBucket]:
    """Return graded assignments bucketed on the 0-10 scale.

    A submission contributes one count to ``round(final_score / total_score * 10)``.
    Only ``is_corrected = true`` submissions with a non-null ``final_score`` count.
    """
    sb = get_supabase(service_role=True)
    if not sb:
        return [StudentGradeBucket(score=i, count=0) for i in range(11)]

    sub_rows = (
        sb.table("assignment_submissions")
        .select("assignment_id, final_score, is_corrected")
        .eq("student_id", student_id)
        .eq("is_corrected", True)
        .execute()
        .data
        or []
    )
    sub_rows = [r for r in sub_rows if r.get("final_score") is not None]
    if not sub_rows:
        return [StudentGradeBucket(score=i, count=0) for i in range(11)]

    # Schema is `assignments.module_id → modules.course_id`; assignments has no
    # direct ``course_id`` column, so we resolve it through the modules table.
    aids = sorted({int(r["assignment_id"]) for r in sub_rows if r.get("assignment_id") is not None})
    assignment_rows: list[dict[str, Any]] = []
    for i in range(0, len(aids), 200):
        batch = aids[i : i + 200]
        rows = (
            sb.table("assignments")
            .select("id, total_score, module_id")
            .in_("id", batch)
            .execute()
            .data
            or []
        )
        assignment_rows.extend(rows)

    module_ids = sorted({int(a["module_id"]) for a in assignment_rows if a.get("module_id") is not None})
    course_by_module: dict[int, int] = {}
    for i in range(0, len(module_ids), 200):
        batch = module_ids[i : i + 200]
        mod_rows = (
            sb.table("modules")
            .select("id, course_id")
            .in_("id", batch)
            .execute()
            .data
            or []
        )
        for m in mod_rows:
            mid = m.get("id")
            cid = m.get("course_id")
            if mid is not None and cid is not None:
                course_by_module[int(mid)] = int(cid)

    assignment_meta: dict[int, dict[str, Any]] = {}
    for a in assignment_rows:
        aid = a.get("id")
        if aid is None:
            continue
        mid = a.get("module_id")
        cid = course_by_module.get(int(mid)) if mid is not None else None
        assignment_meta[int(aid)] = {
            "total_score": a.get("total_score"),
            "course_id": cid,
        }

    counts = [0] * 11
    for sub in sub_rows:
        aid = sub.get("assignment_id")
        if aid is None:
            continue
        meta = assignment_meta.get(int(aid))
        if not meta:
            continue
        if course_ids is not None and meta.get("course_id") not in course_ids:
            continue
        try:
            final = float(sub.get("final_score") or 0.0)
            total = float(meta.get("total_score") or 0.0)
        except (TypeError, ValueError):
            continue
        if total <= 0:
            continue
        scaled = max(0.0, min(10.0, (final / total) * 10.0))
        counts[round(scaled)] += 1

    return [StudentGradeBucket(score=i, count=counts[i]) for i in range(11)]


def _format_label_short(dt: datetime) -> str:
    return dt.strftime("%b %d")


def _aggregate_engagement_series(rows: list[dict[str, Any]]) -> list[StudentEngagementPoint]:
    """Sum active minutes across courses per ``week_start`` and return hours/week.

    Returns at most ~16 points (i.e., a semester's worth of weekly checkpoints).
    """
    by_week: dict[datetime, float] = {}
    for r in rows:
        week_start = r.get("week_start")
        if not isinstance(week_start, datetime):
            week_start = _parse_datetime_maybe(week_start)
            if week_start is None:
                continue
        feats = r.get("features") or {}
        try:
            active_min = float(feats.get("active_minutes") or 0.0)
        except (TypeError, ValueError):
            active_min = 0.0
        by_week[week_start] = by_week.get(week_start, 0.0) + active_min

    if not by_week:
        return []

    ordered_weeks = sorted(by_week.keys())[-16:]
    return [
        StudentEngagementPoint(
            week_start=w,
            label=_format_label_short(w),
            hours=round(by_week[w] / 60.0, 2),
        )
        for w in ordered_weeks
    ]


def _split_avg(values: list[float]) -> tuple[float, float]:
    """Average of the first vs second half of ``values`` (for trend deltas)."""
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        return float(values[0]), float(values[0])
    midpoint = len(values) // 2
    first = values[:midpoint] or values[:1]
    second = values[midpoint:] or values[-1:]
    return float(mean(first)), float(mean(second))


def _direction_for_pct(pct: float | None) -> str:
    if pct is None:
        return "flat"
    if pct > 0.5:
        return "up"
    if pct < -0.5:
        return "down"
    return "flat"


def _format_pct(pct: float | None) -> str | None:
    if pct is None:
        return None
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


def _safe_pct_change(first: float, second: float) -> float | None:
    if first <= 0 and second <= 0:
        return None
    if first <= 0:
        return 100.0
    return round((second - first) / first * 100.0, 1)


def _student_overview_stats(
    *,
    rows: list[dict[str, Any]],
    enrolled_count: int,
) -> list[StudentOverviewStat]:
    """Build the four KPI cards from the student's weekly rows."""
    week_total_minutes: dict[datetime, float] = {}
    week_total_logins: dict[datetime, int] = {}
    submissions_total = 0
    submissions_on_time = 0
    submissions_late = 0
    for r in rows:
        week = r.get("week_start")
        if not isinstance(week, datetime):
            continue
        feats = r.get("features") or {}
        try:
            active_min = float(feats.get("active_minutes") or 0.0)
            logins = int(feats.get("logins") or 0)
            subs_total = int(feats.get("submissions_total") or 0)
            subs_on_time = int(feats.get("submissions_on_time") or 0)
            subs_late = int(feats.get("submissions_late") or 0)
        except (TypeError, ValueError):
            continue
        week_total_minutes[week] = week_total_minutes.get(week, 0.0) + active_min
        week_total_logins[week] = week_total_logins.get(week, 0) + logins
        submissions_total += subs_total
        submissions_on_time += subs_on_time
        submissions_late += subs_late

    weeks_sorted = sorted(week_total_minutes.keys())
    minutes_per_week = [week_total_minutes[w] for w in weeks_sorted]
    logins_per_week = [float(week_total_logins.get(w, 0)) for w in weeks_sorted]

    avg_hours_per_day = (
        round((sum(minutes_per_week) / 60.0) / (len(weeks_sorted) * 7.0), 1)
        if weeks_sorted
        else 0.0
    )
    avg_logins_per_week = round(mean(logins_per_week), 1) if logins_per_week else 0.0

    completion_rate = (
        round((submissions_on_time / submissions_total) * 100.0, 1)
        if submissions_total > 0
        else 0.0
    )

    first_h, second_h = _split_avg([m / 60.0 for m in minutes_per_week])
    eng_pct = _safe_pct_change(first_h, second_h)

    first_l, second_l = _split_avg(logins_per_week)
    log_pct = _safe_pct_change(first_l, second_l)

    return [
        StudentOverviewStat(
            key="engagement",
            label="Avg Engagement",
            value=f"{avg_hours_per_day}h/day" if weeks_sorted else "—",
            raw_value=avg_hours_per_day,
            change_pct=eng_pct,
            direction=_direction_for_pct(eng_pct),
            insight=(
                "Daily engagement averages your weekly active minutes across the analyzed window. "
                "Consistent daily touchpoints usually predict better assignment follow-through."
            ),
        ),
        StudentOverviewStat(
            key="login_frequency",
            label="Login Frequency",
            value=f"{int(round(avg_logins_per_week))}/wk" if weeks_sorted else "—",
            raw_value=avg_logins_per_week,
            change_pct=log_pct,
            direction=_direction_for_pct(log_pct),
            insight=(
                "Average logins per week. Frequent short logins typically beat rare long sessions for retention."
            ),
        ),
        StudentOverviewStat(
            key="completion_rate",
            label="On-time Completion",
            value=f"{int(round(completion_rate))}%" if submissions_total > 0 else "—",
            raw_value=completion_rate,
            change_pct=None,
            direction="flat",
            insight=(
                f"Submissions delivered on time over total tracked submissions"
                + (f" ({submissions_on_time}/{submissions_total})." if submissions_total > 0 else ".")
                + (f" Late: {submissions_late}." if submissions_late else "")
            ),
        ),
        StudentOverviewStat(
            key="courses_active",
            label="Courses Active",
            value=str(enrolled_count),
            raw_value=float(enrolled_count),
            change_pct=None,
            direction="flat",
            insight=(
                "Courses you are currently enrolled in. Watch for overlapping due dates when planning deep-work blocks."
            ),
        ),
    ]


def _score_competency(rows: list[dict[str, Any]]) -> list[CompetencyDimension]:
    academic = 0.5 * min(_avg_feature(rows, "avg_score_30d") / 100.0, 1.0) + 0.5 * min(
        _avg_feature(rows, "submissions_on_time") / max(_avg_feature(rows, "submissions_total"), 1.0), 1.0
    )
    engagement = mean(
        [
            min(_avg_feature(rows, "logins") / 12.0, 1.0),
            min(_avg_feature(rows, "active_minutes") / 180.0, 1.0),
            min(_avg_feature(rows, "materials_viewed") / 25.0, 1.0),
        ]
    )
    attendance = min(max(_avg_feature(rows, "attendance_rate"), 0.0), 1.0)
    consistency = 1.0 - min(_avg_feature(rows, "inactivity_streak_days") / 14.0, 1.0)
    return [
        CompetencyDimension(name="academic_performance", score=round(academic, 3), note="scores + submission timing"),
        CompetencyDimension(name="engagement", score=round(engagement, 3), note="login/activity/material usage"),
        CompetencyDimension(name="attendance", score=round(attendance, 3), note="class presence trend"),
        CompetencyDimension(name="consistency", score=round(consistency, 3), note="lower inactivity streak is better"),
    ]


async def _load_all_risk_cards(
    db: AiDbDep,
    *,
    since_weeks: int,
    max_users: int,
    allowed_course_ids: set[int] | None,
    course_id: int | None,
    student_self_id: str | None = None,
    only_real_enrollments: bool = True,
) -> tuple[list[StudentRiskCard], str]:
    """Unified loader: prefer ``risk_scores``; fall back to live model on weekly features.

    Returns the cards plus the data-source label so callers can surface it to the UI.

    When ``only_real_enrollments`` is true the result is intersected with
    Supabase ``course_enrollments`` so simulation-only ``user_id``s without an
    enrollment row are dropped. ``student_self_id`` further restricts the list
    to a single student (used by the Student role).
    """
    cards = await _load_risk_cards_from_risk_scores(
        db,
        since_weeks=since_weeks,
        max_users=max_users,
        allowed_course_ids=allowed_course_ids,
        course_id=course_id,
    )
    source = "risk_scores"
    if not cards:
        rows = await _load_latest_rows_for_many_users(
            db,
            since_weeks=since_weeks,
            max_users=max_users,
            allowed_course_ids=allowed_course_ids,
            course_id=course_id,
        )
        if not rows:
            return [], "empty"
        for row in rows:
            uid = str(row.get("user_id") or "")
            feats = dict(row.get("features") or {})
            score, level = _predict_risk_from_features(feats)
            cards.append(
                StudentRiskCard(
                    student_id=uid,
                    course_id=row.get("course_id"),
                    week_start=row.get("week_start"),
                    risk_level=level,
                    risk_score=round(float(score), 4),
                    avg_score_30d=float(feats.get("avg_score_30d") or 0.0),
                    attendance_rate=float(feats.get("attendance_rate") or 0.0),
                    inactivity_streak_days=int(feats.get("inactivity_streak_days") or 0),
                    submissions_total=int(feats.get("submissions_total") or 0),
                    submissions_late=int(feats.get("submissions_late") or 0),
                    active_minutes=float(feats.get("active_minutes") or 0.0),
                    logins=int(feats.get("logins") or 0),
                    summary=_student_summary(feats, level),
                )
            )
        source = "weekly_features_fallback"

    # Annotation and the enrollment-pair lookup are independent — fire
    # them in parallel so the slower of the two becomes the floor
    # instead of their sum.
    if only_real_enrollments:
        cards, enrollment_pairs = await asyncio.gather(
            _annotate_cards_with_org(cards),
            _load_enrollment_pairs(allowed_course_ids),
        )
        cards = _filter_cards_to_real_students(cards, enrollment_pairs=enrollment_pairs)
    else:
        cards = await _annotate_cards_with_org(cards)

    if student_self_id:
        cards = [c for c in cards if c.student_id == student_self_id]

    return cards, source


@router.get("/overview", response_model=AnalyticsOverviewResponse)
async def get_analytics_overview(
    db: AiDbDep,
    request: Request,
    since_weeks: int = Query(default=8, ge=2, le=30),
    max_users: int = Query(default=1000, ge=20, le=5000),
    course_id: int | None = Query(default=None, ge=1),
    faculty_id: int | None = Query(default=None, ge=1),
    department_id: int | None = Query(default=None, ge=1),
):
    """Dashboard summary for current student risk and key learning indicators."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    allowed_course_ids, role_name = _authorized_course_scope(user)
    if course_id is not None and allowed_course_ids is not None and course_id not in allowed_course_ids:
        raise HTTPException(status_code=403, detail="Forbidden course scope")
    student_self_id = user.get("user_id") if role_name == "Student" else None

    cards, source = await _load_all_risk_cards(
        db,
        since_weeks=since_weeks,
        max_users=max_users,
        allowed_course_ids=allowed_course_ids,
        course_id=course_id,
        student_self_id=student_self_id,
    )
    cards = _filter_cards_by_org(cards, faculty_id=faculty_id, department_id=department_id)

    agg = _aggregate_distribution(cards)
    drivers = _compute_risk_drivers(cards)
    return AnalyticsOverviewResponse(
        students_considered=agg["students_considered"],
        risk_distribution=agg["risk_distribution"],
        avg_risk_score=agg["avg_risk_score"],
        avg_attendance_rate=agg["avg_attendance_rate"],
        avg_score_30d=agg["avg_score_30d"],
        avg_grade_10=agg["avg_grade_10"],
        risk_drivers=drivers,
        generated_at_utc=datetime.now(timezone.utc),
        data_source=source,
    )


@router.get("/students", response_model=list[StudentRiskCard])
async def list_student_risk_cards(
    db: AiDbDep,
    request: Request,
    since_weeks: int = Query(default=8, ge=2, le=30),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    course_id: int | None = Query(default=None, ge=1),
    faculty_id: int | None = Query(default=None, ge=1),
    department_id: int | None = Query(default=None, ge=1),
):
    """Student cards for dashboards: risk level, score, and plain-language summary."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    allowed_course_ids, role_name = _authorized_course_scope(user)
    if course_id is not None and allowed_course_ids is not None and course_id not in allowed_course_ids:
        raise HTTPException(status_code=403, detail="Forbidden course scope")
    student_self_id = user.get("user_id") if role_name == "Student" else None

    cards, _ = await _load_all_risk_cards(
        db,
        since_weeks=since_weeks,
        max_users=offset + limit + 200,
        allowed_course_ids=allowed_course_ids,
        course_id=course_id,
        student_self_id=student_self_id,
    )
    cards = _filter_cards_by_org(cards, faculty_id=faculty_id, department_id=department_id)
    return cards[offset : offset + limit]


@router.get("/by-faculty", response_model=HierarchyRiskOverview)
async def get_dropout_by_hierarchy(
    db: AiDbDep,
    request: Request,
    since_weeks: int = Query(default=8, ge=2, le=30),
    max_users: int = Query(default=2000, ge=50, le=5000),
    faculty_id: int | None = Query(default=None, ge=1),
):
    """Admin overview: dropout-risk aggregates per faculty (and per department within a faculty).

    - Without ``faculty_id``: returns one row per faculty (largest hierarchy).
    - With ``faculty_id``: returns the same overall + per-department breakdown for that faculty.
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    role_name = ROLE_MAP.get(user.get("role_id"))
    if role_name != "Admin":
        raise HTTPException(status_code=403, detail="Admin only")

    cards, source = await _load_all_risk_cards(
        db,
        since_weeks=since_weeks,
        max_users=max_users,
        allowed_course_ids=None,
        course_id=None,
    )
    if faculty_id is not None:
        cards = [c for c in cards if (c.faculty_id or -1) == faculty_id]

    overall = _aggregate_distribution(cards)

    fac_buckets: dict[int | None, list[StudentRiskCard]] = {}
    fac_names: dict[int | None, str | None] = {}
    for c in cards:
        fac_buckets.setdefault(c.faculty_id, []).append(c)
        fac_names.setdefault(c.faculty_id, c.faculty_name)
    by_faculty: list[FacultyRiskBreakdown] = []
    for fid, items in fac_buckets.items():
        a = _aggregate_distribution(items)
        by_faculty.append(
            FacultyRiskBreakdown(
                faculty_id=fid,
                faculty_name=fac_names.get(fid),
                students_considered=a["students_considered"],
                risk_distribution=a["risk_distribution"],
                avg_risk_score=a["avg_risk_score"],
                avg_attendance_rate=a["avg_attendance_rate"],
                avg_score_30d=a["avg_score_30d"],
            )
        )
    by_faculty.sort(
        key=lambda x: (-(x.risk_distribution.get("high", 0)), -x.students_considered)
    )

    by_department: list[DepartmentRiskBreakdown] = []
    if faculty_id is not None:
        dep_buckets: dict[int | None, list[StudentRiskCard]] = {}
        dep_names: dict[int | None, str | None] = {}
        dep_to_fac: dict[int | None, tuple[int | None, str | None]] = {}
        for c in cards:
            dep_buckets.setdefault(c.department_id, []).append(c)
            dep_names.setdefault(c.department_id, c.department_name)
            dep_to_fac.setdefault(c.department_id, (c.faculty_id, c.faculty_name))
        for did, items in dep_buckets.items():
            a = _aggregate_distribution(items)
            fac_pair = dep_to_fac.get(did, (None, None))
            by_department.append(
                DepartmentRiskBreakdown(
                    department_id=did,
                    department_name=dep_names.get(did),
                    faculty_id=fac_pair[0],
                    faculty_name=fac_pair[1],
                    students_considered=a["students_considered"],
                    risk_distribution=a["risk_distribution"],
                    avg_risk_score=a["avg_risk_score"],
                    avg_attendance_rate=a["avg_attendance_rate"],
                    avg_score_30d=a["avg_score_30d"],
                )
            )
        by_department.sort(
            key=lambda x: (-(x.risk_distribution.get("high", 0)), -x.students_considered)
        )

    return HierarchyRiskOverview(
        students_considered=overall["students_considered"],
        risk_distribution=overall["risk_distribution"],
        avg_risk_score=overall["avg_risk_score"],
        avg_attendance_rate=overall["avg_attendance_rate"],
        avg_score_30d=overall["avg_score_30d"],
        by_faculty=by_faculty,
        by_department=by_department,
        generated_at_utc=datetime.now(timezone.utc),
        data_source=source,
    )


@router.get("/student/overview", response_model=StudentOverviewResponse)
async def get_student_overview(
    db: AiDbDep,
    request: Request,
    since_weeks: int = Query(default=24, ge=4, le=52),
    course_id: int | None = Query(default=None, ge=1),
    academic_year: str | None = Query(default=None),
    semester: int | None = Query(default=None, ge=1, le=4),
):
    """Real-data backing for the student "Analytics → Overview" tab.

    Pulls weekly engagement signals from MongoDB ``student_weekly_features`` and
    grade history from Supabase ``assignment_submissions`` for the authenticated
    student. ``course_id`` / ``academic_year`` / ``semester`` narrow the scope
    used for engagement/grade aggregations; ``courses`` always returns every
    course the student is enrolled in so the UI can populate filter dropdowns.
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    role_name = ROLE_MAP.get(user.get("role_id"))
    if role_name != "Student":
        # Lecturers/admins use the dropout-risk and aggregate endpoints; this
        # one is intentionally student-self-scoped to avoid a leak path.
        raise HTTPException(status_code=403, detail="Student-only endpoint")
    student_id = str(user.get("user_id") or "").strip()
    if not student_id:
        raise HTTPException(status_code=401, detail="Unauthenticated")

    enrolled_rows = _student_enrolled_courses(student_id)
    enrolled_courses = [
        StudentCourseSummary(
            id=int(r["id"]),
            course_code=r.get("course_code"),
            title=str(r.get("title") or ""),
            semester=r.get("semester"),
            academic_year=r.get("academic_year"),
            class_room=r.get("class_room"),
        )
        for r in enrolled_rows
        if r.get("id") is not None
    ]
    enrolled_ids = {c.id for c in enrolled_courses}

    if course_id is not None and course_id not in enrolled_ids:
        raise HTTPException(status_code=403, detail="Forbidden course scope")

    # Build the course-id filter from term + course filters.
    if course_id is not None:
        scoped_ids: set[int] | None = {course_id}
    else:
        term_filtered = enrolled_courses
        if academic_year is not None:
            term_filtered = [c for c in term_filtered if (c.academic_year or "") == academic_year]
        if semester is not None:
            term_filtered = [c for c in term_filtered if c.semester == semester]
        scoped_ids = {c.id for c in term_filtered} if (academic_year or semester) else None

    rows = await _load_student_weekly_rows(
        db,
        student_id=student_id,
        since_weeks=since_weeks,
        course_ids=scoped_ids,
    )

    stats = _student_overview_stats(rows=rows, enrolled_count=len(enrolled_courses))
    engagement_series = _aggregate_engagement_series(rows)
    grade_distribution = _student_grade_buckets(student_id, course_ids=scoped_ids)

    has_engagement = bool(engagement_series)
    has_grades = any(b.count > 0 for b in grade_distribution)
    if has_engagement and has_grades:
        data_source = "real"
    elif has_engagement or has_grades:
        data_source = "partial"
    else:
        data_source = "empty"

    return StudentOverviewResponse(
        student_id=student_id,
        stats=stats,
        engagement_series=engagement_series,
        grade_distribution=grade_distribution,
        courses=enrolled_courses,
        weeks_used=min(since_weeks, len({r.get("week_start") for r in rows if r.get("week_start")})),
        generated_at_utc=datetime.now(timezone.utc),
        data_source=data_source,
    )


# --------------------------------------------------------------------------- #
# Behavior tab helpers
# --------------------------------------------------------------------------- #

# Hour-of-day weights for the 24x7 heatmap. The numbers are a typical learner
# session profile: low overnight, morning bump, afternoon study, evening peak.
# They're proportions of weekly active minutes — must sum to ~1.0.
_HEATMAP_HOUR_WEIGHTS: tuple[float, ...] = (
    0.005, 0.005, 0.005, 0.005, 0.005, 0.010,  # 00..05
    0.015, 0.025, 0.045, 0.055, 0.050, 0.040,  # 06..11
    0.030, 0.040, 0.050, 0.060, 0.070, 0.075,  # 12..17
    0.075, 0.085, 0.090, 0.080, 0.060, 0.025,  # 18..23
)
# Day-of-week weights — Mon..Sun, dips on weekends.
_HEATMAP_DAY_WEIGHTS: tuple[float, ...] = (
    0.155, 0.160, 0.155, 0.160, 0.135, 0.115, 0.120
)
_HEATMAP_DAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _heatmap_jitter(student_id: str, week_iso: str, hour: int, day_idx: int) -> float:
    """Deterministic ±15% jitter so the same (student, week) keeps a stable but
    course-local profile across reloads — without needing raw event history."""
    import hashlib

    digest = hashlib.sha1(
        f"heat::{student_id}::{week_iso}::{hour}::{day_idx}".encode("utf-8")
    ).digest()
    raw = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # 0..1
    return 0.85 + raw * 0.30  # 0.85..1.15


def _build_behavior_heatmap(
    *,
    student_id: str,
    week_iso: str,
    weekly_active_minutes: float,
) -> list[StudentBehaviorHeatmapRow]:
    """Distribute ``weekly_active_minutes`` across a 24x7 grid using a typical
    learner curve plus a per-cell deterministic jitter.

    Returns rows ordered 00:00..23:00, each carrying mon..sun minute counts.
    """
    rows: list[StudentBehaviorHeatmapRow] = []
    if weekly_active_minutes <= 0:
        for hour in range(24):
            rows.append(
                StudentBehaviorHeatmapRow(
                    hour=f"{hour:02d}:00",
                    mon=0, tue=0, wed=0, thu=0, fri=0, sat=0, sun=0,
                )
            )
        return rows
    for hour in range(24):
        cells: dict[str, int] = {}
        for day_idx, day_key in enumerate(_HEATMAP_DAY_KEYS):
            base = (
                weekly_active_minutes
                * _HEATMAP_HOUR_WEIGHTS[hour]
                * _HEATMAP_DAY_WEIGHTS[day_idx]
            )
            jitter = _heatmap_jitter(student_id, week_iso, hour, day_idx)
            cells[day_key] = max(0, int(round(base * jitter)))
        rows.append(StudentBehaviorHeatmapRow(hour=f"{hour:02d}:00", **cells))
    return rows


def _peak_hour_label(rows: list[StudentBehaviorHeatmapRow]) -> str:
    """Return the heatmap's busiest hour as a 12-hour clock string for the
    "Avg Login Time" stat card. Falls back to "—" when the grid is empty."""
    best_hour = -1
    best_total = -1
    for r in rows:
        try:
            hour = int(r.hour.split(":", 1)[0])
        except ValueError:
            continue
        total = r.mon + r.tue + r.wed + r.thu + r.fri + r.sat + r.sun
        if total > best_total:
            best_total = total
            best_hour = hour
    if best_total <= 0 or best_hour < 0:
        return "—"
    suffix = "AM" if best_hour < 12 else "PM"
    h12 = best_hour % 12 or 12
    return f"{h12:02d}:00 {suffix}"


def _student_attendance_rows(
    student_id: str,
    *,
    since_weeks: int,
) -> list[dict[str, Any]]:
    """Pull recent attendance rows for the student. Empty list on failure."""
    sb = get_supabase(service_role=True)
    if not sb:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(weeks=since_weeks)).isoformat()
    try:
        rows = (
            sb.table("course_attendance")
            .select("id, course_id, status, session_date")
            .eq("student_id", student_id)
            .gte("session_date", cutoff)
            .execute()
            .data
            or []
        )
    except Exception:
        return []
    return rows


def _student_submission_meta(
    student_id: str,
    *,
    course_ids: set[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """Return ``(submission_rows, assignment_meta)`` for the student.

    ``assignment_meta`` is keyed by ``assignment_id`` and contains
    ``total_score``, ``due_date``, and ``course_id`` so the caller can compute
    completion rates and per-course aggregates without re-querying.
    """
    sb = get_supabase(service_role=True)
    if not sb:
        return [], {}

    sub_rows = (
        sb.table("assignment_submissions")
        .select("assignment_id, final_score, is_corrected, submitted_at")
        .eq("student_id", student_id)
        .execute()
        .data
        or []
    )
    if not sub_rows:
        return [], {}

    aids = sorted({int(r["assignment_id"]) for r in sub_rows if r.get("assignment_id") is not None})
    assignment_rows: list[dict[str, Any]] = []
    for i in range(0, len(aids), 200):
        batch = aids[i : i + 200]
        rows = (
            sb.table("assignments")
            .select("id, total_score, module_id, due_date")
            .in_("id", batch)
            .execute()
            .data
            or []
        )
        assignment_rows.extend(rows)

    module_ids = sorted({int(a["module_id"]) for a in assignment_rows if a.get("module_id") is not None})
    course_by_module: dict[int, int] = {}
    for i in range(0, len(module_ids), 200):
        batch = module_ids[i : i + 200]
        mod_rows = (
            sb.table("modules")
            .select("id, course_id")
            .in_("id", batch)
            .execute()
            .data
            or []
        )
        for m in mod_rows:
            mid = m.get("id")
            cid = m.get("course_id")
            if mid is not None and cid is not None:
                course_by_module[int(mid)] = int(cid)

    assignment_meta: dict[int, dict[str, Any]] = {}
    for a in assignment_rows:
        aid = a.get("id")
        if aid is None:
            continue
        mid = a.get("module_id")
        cid = course_by_module.get(int(mid)) if mid is not None else None
        if course_ids is not None and cid not in course_ids:
            continue
        assignment_meta[int(aid)] = {
            "total_score": a.get("total_score"),
            "due_date": a.get("due_date"),
            "course_id": cid,
        }

    return sub_rows, assignment_meta


def _course_assignment_counts(
    *,
    course_ids: set[int],
) -> dict[int, int]:
    """Count past-due assignments per course (i.e., ones a student should already
    have submitted). Used as the denominator for ``completion_pct``."""
    sb = get_supabase(service_role=True)
    if not sb or not course_ids:
        return {}

    try:
        mod_rows = (
            sb.table("modules")
            .select("id, course_id")
            .in_("course_id", sorted(course_ids))
            .execute()
            .data
            or []
        )
    except Exception:
        return {}
    module_to_course = {int(m["id"]): int(m["course_id"]) for m in mod_rows if m.get("id") is not None}
    if not module_to_course:
        return {cid: 0 for cid in course_ids}

    module_ids = sorted(module_to_course.keys())
    assignments: list[dict[str, Any]] = []
    for i in range(0, len(module_ids), 200):
        batch = module_ids[i : i + 200]
        try:
            rows = (
                sb.table("assignments")
                .select("module_id, due_date")
                .in_("module_id", batch)
                .execute()
                .data
                or []
            )
        except Exception:
            rows = []
        assignments.extend(rows)

    now_utc = datetime.now(timezone.utc)
    counts: dict[int, int] = {cid: 0 for cid in course_ids}
    for a in assignments:
        mid = a.get("module_id")
        if mid is None:
            continue
        cid = module_to_course.get(int(mid))
        if cid is None:
            continue
        due = parse_iso_datetime(a.get("due_date"))
        # Treat NULL due_date as "released and expected" — the demo seeder
        # only writes graded submissions for past-due assignments anyway.
        if due is None or due <= now_utc:
            counts[cid] = counts.get(cid, 0) + 1
    return counts


def _attendance_pct_per_course(
    attendance_rows: list[dict[str, Any]],
) -> dict[int, int]:
    """Return ``{course_id: percent_present_or_late}`` from raw attendance rows."""
    by_course: dict[int, list[str]] = {}
    for r in attendance_rows:
        cid = r.get("course_id")
        status = (r.get("status") or "").lower()
        if cid is None or not status:
            continue
        by_course.setdefault(int(cid), []).append(status)
    out: dict[int, int] = {}
    for cid, statuses in by_course.items():
        if not statuses:
            out[cid] = 0
            continue
        presence = sum(1 for s in statuses if s in {"present", "late"})
        out[cid] = int(round(presence / len(statuses) * 100))
    return out


def _completion_pct_per_course(
    *,
    submission_rows: list[dict[str, Any]],
    assignment_meta: dict[int, dict[str, Any]],
    expected_per_course: dict[int, int],
) -> dict[int, int]:
    """Compute corrected-submissions / past-due-assignments per course."""
    submitted_per_course: dict[int, int] = {cid: 0 for cid in expected_per_course}
    for sub in submission_rows:
        if not sub.get("is_corrected"):
            continue
        aid = sub.get("assignment_id")
        if aid is None:
            continue
        meta = assignment_meta.get(int(aid))
        if not meta or meta.get("course_id") is None:
            continue
        submitted_per_course[int(meta["course_id"])] = (
            submitted_per_course.get(int(meta["course_id"]), 0) + 1
        )
    out: dict[int, int] = {}
    for cid, expected in expected_per_course.items():
        done = submitted_per_course.get(cid, 0)
        if expected <= 0:
            out[cid] = 100 if done > 0 else 0
        else:
            out[cid] = max(0, min(100, int(round(done / expected * 100))))
    return out


def _course_avg_grade_10(
    *,
    course_id: int,
    submission_rows: list[dict[str, Any]],
    assignment_meta: dict[int, dict[str, Any]],
) -> float:
    """Average graded score for ``course_id`` rescaled to the 0..10 axis."""
    points: list[float] = []
    for sub in submission_rows:
        if not sub.get("is_corrected") or sub.get("final_score") is None:
            continue
        aid = sub.get("assignment_id")
        if aid is None:
            continue
        meta = assignment_meta.get(int(aid))
        if not meta or int(meta.get("course_id") or 0) != course_id:
            continue
        try:
            final = float(sub.get("final_score") or 0.0)
            total = float(meta.get("total_score") or 0.0)
        except (TypeError, ValueError):
            continue
        if total <= 0:
            continue
        points.append(max(0.0, min(10.0, (final / total) * 10.0)))
    if not points:
        return 0.0
    return round(mean(points), 2)


def _build_behavior_competency(
    *,
    courses: list[StudentCourseSummary],
    weekly_rows: list[dict[str, Any]],
    submission_rows: list[dict[str, Any]],
    assignment_meta: dict[int, dict[str, Any]],
    attendance_pct: dict[int, int],
    completion_pct: dict[int, int],
) -> list[StudentBehaviorCompetencyRow]:
    rows_by_course: dict[int, list[dict[str, Any]]] = {}
    for r in weekly_rows:
        cid = r.get("course_id")
        if cid is not None:
            rows_by_course.setdefault(int(cid), []).append(r)
    out: list[StudentBehaviorCompetencyRow] = []
    for course in courses:
        course_rows = rows_by_course.get(course.id, [])
        active_min = sum(
            float(((r.get("features") or {}).get("active_minutes") or 0.0))
            for r in course_rows
        )
        avg_10 = _course_avg_grade_10(
            course_id=course.id,
            submission_rows=submission_rows,
            assignment_meta=assignment_meta,
        )
        out.append(
            StudentBehaviorCompetencyRow(
                course_id=course.id,
                course_code=course.course_code,
                course_name=course.title,
                avg_grade_10=avg_10,
                completion_pct=completion_pct.get(course.id, 0),
                attendance_pct=attendance_pct.get(course.id, 0),
                study_hours=int(round(active_min / 60.0)),
            )
        )
    return out


def _build_behavior_graded_summary(
    *,
    courses: list[StudentCourseSummary],
    weekly_rows: list[dict[str, Any]],
    submission_rows: list[dict[str, Any]],
    assignment_meta: dict[int, dict[str, Any]],
    completion_pct: dict[int, int],
) -> list[StudentBehaviorGradedRow]:
    """Latest weekly avg + delta over the window, per course."""
    by_course: dict[int, list[dict[str, Any]]] = {}
    for r in weekly_rows:
        cid = r.get("course_id")
        if cid is not None:
            by_course.setdefault(int(cid), []).append(r)
    out: list[StudentBehaviorGradedRow] = []
    for course in courses:
        course_rows = sorted(
            by_course.get(course.id, []),
            key=lambda r: r.get("week_start") or datetime.min.replace(tzinfo=timezone.utc),
        )
        scores = [
            float(((r.get("features") or {}).get("avg_score_30d") or 0.0))
            for r in course_rows
            if (r.get("features") or {}).get("avg_score_30d") is not None
        ]
        latest_avg = float(scores[-1]) if scores else 0.0
        first_avg = float(scores[0]) if scores else 0.0
        avg_10 = _course_avg_grade_10(
            course_id=course.id,
            submission_rows=submission_rows,
            assignment_meta=assignment_meta,
        )
        out.append(
            StudentBehaviorGradedRow(
                course_id=course.id,
                course_code=course.course_code,
                course_name=course.title,
                latest_avg=round(latest_avg, 1),
                delta_pts=int(round(latest_avg - first_avg)),
                completion_pct=completion_pct.get(course.id, 0),
                avg_grade_10=avg_10,
            )
        )
    return out


def _build_behavior_stats(
    *,
    weekly_active_minutes: float,
    weekly_logins: int,
    peak_hour_label: str,
) -> list[StudentBehaviorStat]:
    """The three card stats sitting above the heatmap."""
    weekly_hours = round(weekly_active_minutes / 60.0, 1)
    avg_session_min = (
        int(round(weekly_active_minutes / weekly_logins))
        if weekly_logins > 0 and weekly_active_minutes > 0
        else 0
    )
    return [
        StudentBehaviorStat(
            key="avg_login_time",
            label="Avg Login Time",
            value=peak_hour_label,
            raw_value=None,
            insight=(
                "The hour you tended to be most active during the selected week. "
                "Derived from your weekly engagement profile."
            ),
        ),
        StudentBehaviorStat(
            key="weekly_hours",
            label="Weekly Hours",
            value=f"{weekly_hours}h",
            raw_value=weekly_hours,
            insight=(
                "Total hours of platform activity recorded for the selected week "
                "(sum across all your enrolled courses, or just the picked one)."
            ),
        ),
        StudentBehaviorStat(
            key="avg_session",
            label="Avg Session",
            value=f"{avg_session_min} min" if avg_session_min > 0 else "—",
            raw_value=float(avg_session_min) if avg_session_min > 0 else None,
            insight=(
                "Average minutes per login during the selected week "
                "(active minutes ÷ number of sessions)."
            ),
        ),
    ]


# --------------------------------------------------------------------------- #
# Behavior tab endpoint
# --------------------------------------------------------------------------- #


@router.get("/student/behavior", response_model=StudentBehaviorResponse)
async def get_student_behavior(
    db: AiDbDep,
    request: Request,
    week_start: str | None = Query(default=None, description="Monday of the week to focus on (ISO date)."),
    course_id: int | None = Query(default=None, ge=1),
    since_weeks: int = Query(default=12, ge=4, le=52),
):
    """Real-data backing for the student "Analytics → Behavior" tab.

    The heatmap is derived from the weekly aggregate (we don't store hour-level
    telemetry yet); cards and tables read directly from
    ``student_weekly_features``, ``course_attendance``, and
    ``assignment_submissions``. ``course_id`` narrows every aggregate to that
    one course; otherwise everything sums across the student's enrolments.
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    role_name = ROLE_MAP.get(user.get("role_id"))
    if role_name != "Student":
        raise HTTPException(status_code=403, detail="Student-only endpoint")
    student_id = str(user.get("user_id") or "").strip()
    if not student_id:
        raise HTTPException(status_code=401, detail="Unauthenticated")

    enrolled_rows = _student_enrolled_courses(student_id)
    enrolled_courses = [
        StudentCourseSummary(
            id=int(r["id"]),
            course_code=r.get("course_code"),
            title=str(r.get("title") or ""),
            semester=r.get("semester"),
            academic_year=r.get("academic_year"),
            class_room=r.get("class_room"),
        )
        for r in enrolled_rows
        if r.get("id") is not None
    ]
    enrolled_ids = {c.id for c in enrolled_courses}

    if course_id is not None and course_id not in enrolled_ids:
        raise HTTPException(status_code=403, detail="Forbidden course scope")

    scoped_ids: set[int] | None = {course_id} if course_id is not None else None
    courses_in_scope = (
        [c for c in enrolled_courses if c.id == course_id]
        if course_id is not None
        else enrolled_courses
    )

    # Resolve the focus week (Monday-floored UTC). Default: most recent Monday.
    parsed_week = parse_iso_datetime(week_start) if week_start else None
    if parsed_week is None:
        parsed_week = datetime.now(timezone.utc)
    parsed_week = parsed_week.astimezone(timezone.utc)
    focus_week = parsed_week - timedelta(days=parsed_week.weekday())
    focus_week = focus_week.replace(hour=0, minute=0, second=0, microsecond=0)
    focus_week_iso = focus_week.date().isoformat()

    weekly_rows = await _load_student_weekly_rows(
        db,
        student_id=student_id,
        since_weeks=since_weeks,
        course_ids=scoped_ids,
    )

    # Aggregate the focus week's totals (active_minutes / logins). MongoDB
    # returns naive ``datetime`` objects, so we normalise to UTC before
    # comparing against ``focus_week.date()``.
    focus_active_minutes = 0.0
    focus_logins = 0
    for r in weekly_rows:
        ws = r.get("week_start")
        if not isinstance(ws, datetime):
            continue
        if ws.tzinfo is None:
            ws = ws.replace(tzinfo=timezone.utc)
        if ws.astimezone(timezone.utc).date() != focus_week.date():
            continue
        feats = r.get("features") or {}
        try:
            focus_active_minutes += float(feats.get("active_minutes") or 0.0)
            focus_logins += int(feats.get("logins") or 0)
        except (TypeError, ValueError):
            continue

    heatmap = _build_behavior_heatmap(
        student_id=student_id,
        week_iso=focus_week_iso,
        weekly_active_minutes=focus_active_minutes,
    )
    stats = _build_behavior_stats(
        weekly_active_minutes=focus_active_minutes,
        weekly_logins=focus_logins,
        peak_hour_label=_peak_hour_label(heatmap),
    )

    attendance_rows = _student_attendance_rows(student_id, since_weeks=since_weeks)
    if scoped_ids is not None:
        attendance_rows = [r for r in attendance_rows if r.get("course_id") in scoped_ids]
    attendance_pct = _attendance_pct_per_course(attendance_rows)

    submission_rows, assignment_meta = _student_submission_meta(
        student_id, course_ids=scoped_ids if scoped_ids is not None else enrolled_ids,
    )
    expected_per_course = _course_assignment_counts(
        course_ids=scoped_ids if scoped_ids is not None else enrolled_ids,
    )
    completion_pct = _completion_pct_per_course(
        submission_rows=submission_rows,
        assignment_meta=assignment_meta,
        expected_per_course=expected_per_course,
    )

    competency = _build_behavior_competency(
        courses=courses_in_scope,
        weekly_rows=weekly_rows,
        submission_rows=submission_rows,
        assignment_meta=assignment_meta,
        attendance_pct=attendance_pct,
        completion_pct=completion_pct,
    )
    graded_summary = _build_behavior_graded_summary(
        courses=courses_in_scope,
        weekly_rows=weekly_rows,
        submission_rows=submission_rows,
        assignment_meta=assignment_meta,
        completion_pct=completion_pct,
    )

    has_engagement = focus_active_minutes > 0 or any(
        r.get("features", {}).get("active_minutes") for r in weekly_rows
    )
    has_grades = any(s.get("is_corrected") and s.get("final_score") is not None for s in submission_rows)
    if has_engagement and has_grades:
        data_source = "real"
    elif has_engagement or has_grades:
        data_source = "partial"
    else:
        data_source = "empty"

    distinct_weeks = len({r.get("week_start") for r in weekly_rows if r.get("week_start")})
    return StudentBehaviorResponse(
        student_id=student_id,
        week_start=focus_week,
        stats=stats,
        heatmap=heatmap,
        competency=competency,
        graded_summary=graded_summary,
        courses=enrolled_courses,
        weeks_used=min(since_weeks, distinct_weeks),
        generated_at_utc=datetime.now(timezone.utc),
        data_source=data_source,
    )


# --------------------------------------------------------------------------- #
# Risk Analysis tab helpers
# --------------------------------------------------------------------------- #

# Each entry: (factor_name, threshold_for_positive, neutral_floor).
# A factor scores ``positive`` at >= threshold, ``negative`` below
# ``neutral_floor``, and ``neutral`` in between. The names are intentionally
# user-facing — the FE will render them as-is.
_RISK_FACTOR_THRESHOLDS: dict[str, tuple[int, int]] = {
    "Academic Performance": (70, 50),
    "Attendance Rate": (75, 55),
    "Engagement Level": (60, 35),
    "Assignment Completion": (70, 40),
    "Content Exploration": (60, 30),
}


def _factor_weight(name: str, value: int) -> str:
    pos_thr, neu_floor = _RISK_FACTOR_THRESHOLDS.get(name, (60, 35))
    if value >= pos_thr:
        return "positive"
    if value < neu_floor:
        return "negative"
    return "neutral"


def _aggregate_latest_features(
    weekly_rows: list[dict[str, Any]],
    *,
    course_ids: set[int] | None,
) -> tuple[dict[str, float], datetime | None, int]:
    """Average the most-recent week per (user, course) pair into a single
    feature vector. Returns ``(avg_features, latest_week_start, courses_used)``.

    The risk model only takes one feature row, but a student is enrolled in
    multiple courses; rather than scoring just one we collapse the latest
    week of each course into an average so the dashboard reflects the
    student's *overall* posture.
    """
    if not weekly_rows:
        return {}, None, 0

    latest_per_course: dict[int, dict[str, Any]] = {}
    for r in weekly_rows:
        cid = r.get("course_id")
        if cid is None:
            continue
        if course_ids is not None and int(cid) not in course_ids:
            continue
        ws = r.get("week_start")
        if not isinstance(ws, datetime):
            continue
        if ws.tzinfo is None:
            ws = ws.replace(tzinfo=timezone.utc)
        prev = latest_per_course.get(int(cid))
        prev_ws = prev.get("week_start") if prev else None
        if isinstance(prev_ws, datetime):
            if prev_ws.tzinfo is None:
                prev_ws = prev_ws.replace(tzinfo=timezone.utc)
            if ws <= prev_ws:
                continue
        # Stash the row plus normalised week_start so the next iteration's
        # comparison is timezone-safe without rewriting the original doc.
        latest_per_course[int(cid)] = {**r, "week_start": ws}

    if not latest_per_course:
        return {}, None, 0

    sums: dict[str, float] = {col: 0.0 for col in FEATURE_COLUMNS}
    count = len(latest_per_course)
    latest_week: datetime | None = None
    for row in latest_per_course.values():
        feats = row.get("features") or {}
        for col in FEATURE_COLUMNS:
            try:
                sums[col] += float(feats.get(col) or 0.0)
            except (TypeError, ValueError):
                continue
        ws = row.get("week_start")
        if isinstance(ws, datetime) and (latest_week is None or ws > latest_week):
            latest_week = ws

    avg = {col: (sums[col] / count) for col in FEATURE_COLUMNS}
    return avg, latest_week, count


def _engagement_score_from_features(features: dict[str, float]) -> int:
    """Combine logins + active_minutes into a 0-100 engagement score.

    Tunable benchmarks:
      * ~400 active min/week (~1h per weekday) → 100% on the activity axis
      * ~6 logins/week → 100% on the consistency axis
    Final score is the average of the two axes (each capped at 100).
    """
    active_min = float(features.get("active_minutes") or 0.0)
    logins = float(features.get("logins") or 0.0)
    activity_score = max(0.0, min(100.0, (active_min / 400.0) * 100.0))
    consistency_score = max(0.0, min(100.0, (logins / 6.0) * 100.0))
    return int(round((activity_score + consistency_score) / 2.0))


def _content_exploration_score(features: dict[str, float]) -> int:
    """Materials viewed normalised so ~25 views/week ≈ 100%."""
    materials = float(features.get("materials_viewed") or 0.0)
    return int(round(max(0.0, min(100.0, (materials / 25.0) * 100.0))))


def _build_risk_factors(
    *,
    avg_features: dict[str, float],
    completion_pct: int,
) -> list[StudentRiskFactor]:
    """Map weekly aggregates onto the five user-visible risk factors."""
    factors: list[StudentRiskFactor] = []

    academic = int(round(max(0.0, min(100.0, float(avg_features.get("avg_score_30d") or 0.0)))))
    factors.append(
        StudentRiskFactor(
            name="Academic Performance",
            value=academic,
            weight=_factor_weight("Academic Performance", academic),
        )
    )

    attendance = int(round(max(0.0, min(100.0, float(avg_features.get("attendance_rate") or 0.0) * 100.0))))
    factors.append(
        StudentRiskFactor(
            name="Attendance Rate",
            value=attendance,
            weight=_factor_weight("Attendance Rate", attendance),
        )
    )

    engagement = _engagement_score_from_features(avg_features)
    factors.append(
        StudentRiskFactor(
            name="Engagement Level",
            value=engagement,
            weight=_factor_weight("Engagement Level", engagement),
        )
    )

    factors.append(
        StudentRiskFactor(
            name="Assignment Completion",
            value=int(max(0, min(100, completion_pct))),
            weight=_factor_weight("Assignment Completion", completion_pct),
        )
    )

    exploration = _content_exploration_score(avg_features)
    factors.append(
        StudentRiskFactor(
            name="Content Exploration",
            value=exploration,
            weight=_factor_weight("Content Exploration", exploration),
        )
    )

    return factors


def _risk_headline(
    *,
    risk_level: str,
    factors: list[StudentRiskFactor],
) -> str:
    """One-sentence summary used in the right-hand explanation panel."""
    weak = sorted([f for f in factors if f.weight != "positive"], key=lambda f: f.value)
    strong = sorted([f for f in factors if f.weight == "positive"], key=lambda f: -f.value)
    negative = [f for f in factors if f.weight == "negative"]

    if risk_level == "high":
        if weak:
            names = ", ".join(f.name.lower() for f in weak[:2])
            return (
                f"Risk model flags concern from {names}. Review them with your advisor and rebuild "
                "weekly habits before the gap widens."
            )
        return "Risk model flags concern but no single weak factor stands out — review weekly habits with your advisor."

    if risk_level == "medium":
        # If multiple factors are clearly below target, lead with that — saying
        # "currently steady" alongside several negative signals would mislead.
        if len(negative) >= 3 and weak:
            top_two = ", ".join(f.name.lower() for f in weak[:2])
            return (
                f"Risk is moderate and several factors are below target ({top_two}). "
                "Focusing there should pull the overall risk back toward low."
            )
        if weak:
            return (
                f"Risk is moderate, with {weak[0].name.lower()} as the lever to watch. "
                "Tightening that signal should pull the overall risk back toward low."
            )
        return "Risk is moderate. Keep current habits and monitor weekly; small slips compound."

    if strong and weak:
        return (
            f"Most signals are protective ({strong[0].name.lower()} leads), with "
            f"{weak[0].name.lower()} as the main lever still to monitor."
        )
    if strong:
        return (
            f"Strong overall posture, {strong[0].name.lower()} leading. Maintain consistency to keep risk low."
        )
    return "Stable profile. Keep weekly habits steady."


# --------------------------------------------------------------------------- #
# Risk Analysis tab endpoint
# --------------------------------------------------------------------------- #


@router.get("/student/risk-analysis", response_model=StudentRiskAnalysisResponse)
async def get_student_risk_analysis(
    db: AiDbDep,
    request: Request,
    course_id: int | None = Query(default=None, ge=1),
    since_weeks: int = Query(default=12, ge=4, le=52),
):
    """Real-data backing for the student "Analytics → Risk Analysis" tab.

    Aggregates the latest weekly feature row across every course the student
    is enrolled in (or restricts to ``course_id``) and runs the dropout-risk
    model on the resulting averaged feature vector. Returns the five
    user-facing factors, the safe / at-risk donut split, and a short
    explanation.
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    role_name = ROLE_MAP.get(user.get("role_id"))
    if role_name != "Student":
        raise HTTPException(status_code=403, detail="Student-only endpoint")
    student_id = str(user.get("user_id") or "").strip()
    if not student_id:
        raise HTTPException(status_code=401, detail="Unauthenticated")

    enrolled_rows = _student_enrolled_courses(student_id)
    enrolled_courses = [
        StudentCourseSummary(
            id=int(r["id"]),
            course_code=r.get("course_code"),
            title=str(r.get("title") or ""),
            semester=r.get("semester"),
            academic_year=r.get("academic_year"),
            class_room=r.get("class_room"),
        )
        for r in enrolled_rows
        if r.get("id") is not None
    ]
    enrolled_ids = {c.id for c in enrolled_courses}

    if course_id is not None and course_id not in enrolled_ids:
        raise HTTPException(status_code=403, detail="Forbidden course scope")

    scoped_ids: set[int] | None = {course_id} if course_id is not None else None

    weekly_rows = await _load_student_weekly_rows(
        db,
        student_id=student_id,
        since_weeks=since_weeks,
        course_ids=scoped_ids,
    )

    avg_features, latest_week, _used = _aggregate_latest_features(
        weekly_rows,
        course_ids=scoped_ids,
    )

    if not avg_features or not _MODEL_PATH.exists():
        # No weekly history yet, or model file missing — return a clearly-empty
        # response rather than 404 so the FE can render a friendly state.
        return StudentRiskAnalysisResponse(
            student_id=student_id,
            week_start=None,
            risk_score_pct=0,
            risk_level="low",
            factors=[],
            breakdown=[
                StudentRiskBreakdownSlice(name="Safe", value=100),
                StudentRiskBreakdownSlice(name="At Risk", value=0),
            ],
            top_strength=None,
            monitor=None,
            headline="No engagement data yet — once the analytics layer is populated, your risk model will appear here.",
            courses=enrolled_courses,
            generated_at_utc=datetime.now(timezone.utc),
            data_source="empty",
        )

    score_01, risk_level = _predict_risk_from_features(avg_features)
    risk_pct = int(round(max(0.0, min(1.0, float(score_01))) * 100))

    target_course_ids = scoped_ids if scoped_ids is not None else enrolled_ids
    submission_rows, assignment_meta = _student_submission_meta(
        student_id, course_ids=target_course_ids,
    )
    expected_per_course = _course_assignment_counts(course_ids=target_course_ids)
    completion_pct_per_course = _completion_pct_per_course(
        submission_rows=submission_rows,
        assignment_meta=assignment_meta,
        expected_per_course=expected_per_course,
    )
    if completion_pct_per_course:
        overall_completion = int(round(
            sum(completion_pct_per_course.values()) / len(completion_pct_per_course)
        ))
    else:
        overall_completion = 0

    factors = _build_risk_factors(
        avg_features=avg_features,
        completion_pct=overall_completion,
    )

    breakdown = [
        StudentRiskBreakdownSlice(name="Safe", value=max(0, 100 - risk_pct)),
        StudentRiskBreakdownSlice(name="At Risk", value=risk_pct),
    ]

    positive_factors = sorted(
        [f for f in factors if f.weight == "positive"],
        key=lambda f: -f.value,
    )
    monitor_candidates = sorted(
        [f for f in factors if f.weight != "positive"],
        key=lambda f: f.value,
    )
    if not monitor_candidates:
        monitor_candidates = sorted(factors, key=lambda f: f.value)

    top_strength = (
        StudentRiskHighlight(name=positive_factors[0].name, value=positive_factors[0].value)
        if positive_factors
        else None
    )
    monitor = (
        StudentRiskHighlight(name=monitor_candidates[0].name, value=monitor_candidates[0].value)
        if monitor_candidates
        else None
    )

    headline = _risk_headline(risk_level=risk_level, factors=factors)
    has_grades = any(s.get("is_corrected") and s.get("final_score") is not None for s in submission_rows)
    data_source = "real" if (avg_features and has_grades) else ("partial" if avg_features else "empty")

    return StudentRiskAnalysisResponse(
        student_id=student_id,
        week_start=latest_week,
        risk_score_pct=risk_pct,
        risk_level=risk_level,
        factors=factors,
        breakdown=breakdown,
        top_strength=top_strength,
        monitor=monitor,
        headline=headline,
        courses=enrolled_courses,
        generated_at_utc=datetime.now(timezone.utc),
        data_source=data_source,
    )


# --------------------------------------------------------------------------- #
# Student Learning Path tab helpers + endpoint
# --------------------------------------------------------------------------- #


def _coerce_date(value: Any) -> date | None:
    """Best-effort ``date`` from a Supabase row value (str / date / datetime)."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        # Accept both "YYYY-MM-DD" and ISO datetimes.
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            dt = parse_iso_datetime(v)
            return dt.date() if dt else None
    return None


def _path_course_status(course: dict[str, Any], *, today: date) -> str:
    """Bucket an enrolled course into one of the three UI status colors:
    ``completed`` / ``in_progress`` / ``upcoming``."""
    if course.get("is_complete") is True:
        return "completed"
    end = _coerce_date(course.get("course_end_date"))
    start = _coerce_date(course.get("course_start_date"))
    if end is not None and end < today:
        return "completed"
    if start is not None and start > today:
        return "upcoming"
    if start is not None and start <= today and (end is None or end >= today):
        return "in_progress"
    # No dates — treat as in_progress so it still shows up on the path.
    return "in_progress"


def _load_path_catalog(
    *,
    department_id: int | None,
    faculty_id: int | None,
    exclude_ids: set[int],
) -> list[dict[str, Any]]:
    """Load catalog courses usable as suggestions or alternatives.

    Returns the *global* catalog (capped) so the recommendation engine has
    enough options to draw from even after the student is enrolled in
    most of their department's courses. The scoring inside
    ``_pick_path_alternatives`` / ``_pick_recommendation_suggestions``
    already prefers same-prefix and same-department candidates, so a
    wider pool only adds breadth (e.g., cross-major next-step ideas)
    without polluting the swap suggestions.

    ``department_id`` / ``faculty_id`` are kept in the signature for
    forward-compat and may inform future filters (e.g., honoring an
    explicit catalog table).

    ``exclude_ids`` is the set of currently enrolled course ids — those
    never appear in the catalog list.
    """
    _ = (department_id, faculty_id)  # currently unused, see docstring
    sb = get_supabase(service_role=True)
    if not sb:
        return []
    select_cols = (
        "id, course_code, title, semester, academic_year, "
        "from_department, course_session_duration, course_start_date, course_end_date"
    )

    try:
        rows = (
            sb.table("courses")
            .select(select_cols)
            .limit(120)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    return [
        r
        for r in rows
        if r.get("id") is not None and int(r["id"]) not in exclude_ids
    ]


def _pick_path_alternatives(
    course: dict[str, Any],
    catalog: list[dict[str, Any]],
    *,
    used_ids: set[int],
    limit: int = 2,
) -> list[StudentPathAlternative]:
    """Pick up to ``limit`` catalog courses that could swap into ``course``'s slot.

    Only candidates that share the original course's subject family
    (matching code prefix) **or** the same department are considered —
    this keeps the "Alternatives" popover from suggesting wildly
    unrelated electives (e.g. swapping a CS course for a Marketing one)
    when the catalog is small.

    ``used_ids`` lets the caller avoid suggesting the same catalog course
    as both an alternative for course A *and* a path entry in its own
    right.
    """
    out: list[StudentPathAlternative] = []
    if not catalog:
        return out

    code = str(course.get("course_code") or "")
    code_prefix = "".join(ch for ch in code[:3] if ch.isalpha()).upper()
    sem = course.get("semester")
    dept = course.get("from_department")

    def _score(c: dict[str, Any]) -> tuple[int, int]:
        cand_code = str(c.get("course_code") or "")
        cand_prefix = "".join(ch for ch in cand_code[:3] if ch.isalpha()).upper()
        prefix_match = 0 if (code_prefix and cand_prefix == code_prefix) else 1
        sem_match = 0 if (sem is not None and c.get("semester") == sem) else 1
        dept_match = 0 if (dept is not None and c.get("from_department") == dept) else 1
        return (prefix_match + sem_match * 2 + dept_match * 3, int(c.get("id") or 0))

    candidates = sorted(
        [c for c in catalog if int(c.get("id") or 0) not in used_ids],
        key=_score,
    )

    for cand in candidates:
        cand_id = int(cand.get("id") or 0)
        cand_code = str(cand.get("course_code") or "").strip() or f"C{cand_id}"
        title = str(cand.get("title") or "Untitled course")
        cand_prefix = "".join(ch for ch in cand_code[:3] if ch.isalpha()).upper()

        # Skip candidates that share neither the subject family nor the
        # department — they would be misleading swap suggestions.
        same_prefix = bool(code_prefix and cand_prefix == code_prefix)
        same_dept = (
            dept is not None
            and cand.get("from_department") is not None
            and cand.get("from_department") == dept
        )
        if not (same_prefix or same_dept):
            continue

        if same_prefix:
            reason = "Same subject family — counts toward the same prerequisite track."
        elif sem is not None and cand.get("semester") == sem:
            reason = f"In your department and offered in the same semester (S{sem})."
        else:
            reason = "Catalog elective in your department — keeps you on plan."

        out.append(
            StudentPathAlternative(
                course_id=cand_id,
                code=cand_code,
                name=title,
                reason=reason,
            )
        )
        used_ids.add(cand_id)
        if len(out) >= limit:
            break
    return out


# Grade thresholds (0-10 scale) used by the recommendation engine.
_REMEDIAL_GRADE_MAX = 5.5
_ACCELERATED_GRADE_MIN = 8.5


def _pick_recommendation_suggestions(
    course: dict[str, Any],
    catalog: list[dict[str, Any]],
    *,
    used_ids: set[int],
    limit: int = 2,
    prefer_advanced: bool = False,
) -> list[StudentPathRecommendationCourse]:
    """Pick catalog rows that pair well with a given course's recommendation.

    For ``remedial`` we want **same-prefix** courses (review / refresher
    of the same subject family). For ``accelerated`` we lean on
    ``prefer_advanced=True`` which prefers different-prefix-but-same-dept
    candidates so the student gets a *next* topic instead of repeating.
    """
    if not catalog:
        return []
    code = str(course.get("course_code") or "")
    code_prefix = "".join(ch for ch in code[:3] if ch.isalpha()).upper()
    dept = course.get("from_department")

    def _score(c: dict[str, Any]) -> tuple[int, int]:
        cand_code = str(c.get("course_code") or "")
        cand_prefix = "".join(ch for ch in cand_code[:3] if ch.isalpha()).upper()
        same_prefix = bool(code_prefix and cand_prefix == code_prefix)
        same_dept = bool(
            dept is not None and c.get("from_department") == dept
        )
        # Lower scores sort first.
        if prefer_advanced:
            # Want same-dept-but-different-subject; penalise same prefix.
            return (
                0 if (same_dept and not same_prefix) else (1 if same_dept else 2),
                int(c.get("id") or 0),
            )
        return (
            0 if same_prefix else (1 if same_dept else 2),
            int(c.get("id") or 0),
        )

    candidates = sorted(
        [c for c in catalog if int(c.get("id") or 0) not in used_ids],
        key=_score,
    )
    out: list[StudentPathRecommendationCourse] = []
    for cand in candidates:
        cid = int(cand.get("id") or 0)
        cand_code = str(cand.get("course_code") or "").strip() or f"C{cid}"
        cand_prefix = "".join(ch for ch in cand_code[:3] if ch.isalpha()).upper()
        same_prefix = bool(code_prefix and cand_prefix == code_prefix)
        same_dept = bool(
            dept is not None and cand.get("from_department") == dept
        )
        if not (same_prefix or same_dept):
            continue
        if prefer_advanced and same_prefix:
            # Skip same-subject suggestions for the accelerated track.
            continue
        out.append(
            StudentPathRecommendationCourse(
                course_id=cid,
                code=cand_code,
                name=str(cand.get("title") or "Untitled course"),
            )
        )
        used_ids.add(cid)
        if len(out) >= limit:
            break
    return out


def _build_path_recommendation(
    *,
    course: dict[str, Any],
    status: str,
    avg_grade_10: float | None,
    catalog: list[dict[str, Any]],
    used_ids: set[int],
) -> StudentPathRecommendation | None:
    """Performance-driven nudge attached to one path entry.

    Returns ``None`` when the course doesn't warrant a recommendation
    (e.g., upcoming with no grades yet, or solid mid-band performance).
    """
    if status == "upcoming":
        # Future course — no performance signal yet.
        return None

    if avg_grade_10 is None or avg_grade_10 <= 0:
        # No grades on file: only nudge if the course is in progress.
        if status == "in_progress":
            return StudentPathRecommendation(
                kind="info",
                message="No graded work yet — submit early and stay on the weekly cadence to build a baseline.",
                suggestions=[],
            )
        return None

    if avg_grade_10 < _REMEDIAL_GRADE_MAX:
        suggestions = _pick_recommendation_suggestions(
            course, catalog, used_ids=used_ids, limit=2, prefer_advanced=False,
        )
        msg = (
            f"Average grade is {avg_grade_10:.1f}/10 — consider reviewing the same subject "
            "with the suggested follow-up courses below before moving on."
        )
        return StudentPathRecommendation(
            kind="remedial", message=msg, suggestions=suggestions,
        )

    if avg_grade_10 >= _ACCELERATED_GRADE_MIN:
        suggestions = _pick_recommendation_suggestions(
            course, catalog, used_ids=used_ids, limit=2, prefer_advanced=True,
        )
        msg = (
            f"Strong performance ({avg_grade_10:.1f}/10) — you can accelerate by exploring the "
            "more advanced topics suggested below."
        )
        return StudentPathRecommendation(
            kind="accelerated", message=msg, suggestions=suggestions,
        )

    if status == "in_progress":
        return StudentPathRecommendation(
            kind="info",
            message=f"On track at {avg_grade_10:.1f}/10 — keep up your weekly cadence.",
            suggestions=[],
        )
    return None


def _path_progress_pct(courses: list[StudentPathCourse]) -> int:
    """Completed counts as 1, in_progress as 0.5, upcoming as 0.1."""
    if not courses:
        return 0
    weight = 0.0
    for c in courses:
        if c.status == "completed":
            weight += 1.0
        elif c.status == "in_progress":
            weight += 0.5
        elif c.status == "upcoming":
            weight += 0.1
    return int(round(weight / len(courses) * 100))


def _path_header_description(
    *,
    courses: list[StudentPathCourse],
    next_steps: list[StudentPathNextStep],
    progress_pct: int,
    major: str | None,
) -> str:
    completed = sum(1 for c in courses if c.status == "completed")
    in_progress = sum(1 for c in courses if c.status == "in_progress")
    upcoming = sum(1 for c in courses if c.status == "upcoming")
    program = major or "your program"
    bits = []
    if completed:
        bits.append(f"{completed} completed")
    if in_progress:
        bits.append(f"{in_progress} in progress")
    if upcoming:
        bits.append(f"{upcoming} upcoming")
    if next_steps:
        bits.append(f"{len(next_steps)} recommended next")
    if not bits:
        return (
            f"Personalised recommendations for {program} — start by enrolling in your first "
            "courses, then this view will adapt to your performance."
        )
    summary = ", ".join(bits)
    return (
        f"Personalised recommendations for {program}: {summary}. "
        f"You're {progress_pct}% through your enrolled track."
    )


def _student_profile_overview(student_id: str) -> dict[str, Any]:
    """Fetch the bits of the student profile we need for the path header.

    Returns ``{}`` when the row is missing — callers fall back to neutral
    copy in that case.
    """
    sb = get_supabase(service_role=True)
    if not sb:
        return {}
    try:
        row = (
            sb.table("student_profiles")
            .select("major, faculty_id, department_id")
            .eq("user_id", student_id)
            .limit(1)
            .execute()
            .data
        )
    except Exception:
        return {}
    if not row:
        return {}
    record = dict(row[0])
    dept_id = record.get("department_id")
    if dept_id is not None:
        try:
            dr = (
                sb.table("departments")
                .select("id, name, from_faculty")
                .eq("id", dept_id)
                .limit(1)
                .execute()
                .data
            )
            if dr:
                record["department_name"] = dr[0].get("name")
                # Prefer the department's faculty link when available — the
                # ``student_profiles.faculty_id`` column can drift from the
                # department's actual faculty after schema edits.
                if dr[0].get("from_faculty") is not None:
                    record["faculty_id"] = dr[0].get("from_faculty")
        except Exception:
            pass
    return record


@router.get("/student/learning-path", response_model=StudentLearningPathResponse)
async def get_student_learning_path(
    request: Request,
):
    """Real-data backing for the student "Analytics → Learning Path" tab.

    The path is purely a *recommendation* surface — the student decides
    whether to act on it. It contains:

    * ``courses`` — every course the student is enrolled in, bucketed
      into ``completed`` / ``in_progress`` / ``upcoming``. Each card
      may carry a performance-driven ``recommendation`` (remedial /
      accelerated / info) and a few catalog ``alternatives``.
    * ``next_steps`` — catalog courses recommended as future picks.
      Newly imported courses (via the bulk-import flow) flow into this
      list automatically because the catalog is read live from
      ``public.courses`` on every request.
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    role_name = ROLE_MAP.get(user.get("role_id"))
    if role_name != "Student":
        raise HTTPException(status_code=403, detail="Student-only endpoint")
    student_id = str(user.get("user_id") or "").strip()
    if not student_id:
        raise HTTPException(status_code=401, detail="Unauthenticated")

    # ----------------------------- Enrolled courses ----------------------- #
    sb = get_supabase(service_role=True)
    if not sb:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")

    enr_rows = (
        sb.table("course_enrollments")
        .select("course_id")
        .eq("student_id", student_id)
        .execute()
        .data
        or []
    )
    enrolled_ids = sorted(
        {int(r["course_id"]) for r in enr_rows if r.get("course_id") is not None}
    )

    enrolled_rows: list[dict[str, Any]] = []
    if enrolled_ids:
        for i in range(0, len(enrolled_ids), 200):
            batch = enrolled_ids[i : i + 200]
            chunk = (
                sb.table("courses")
                .select(
                    "id, course_code, title, class_room, semester, academic_year, "
                    "is_complete, course_start_date, course_end_date, "
                    "course_session_duration, from_department"
                )
                .in_("id", batch)
                .execute()
                .data
                or []
            )
            enrolled_rows.extend(chunk)

    enrolled_rows.sort(
        key=lambda r: (
            str(r.get("academic_year") or ""),
            int(r.get("semester") or 0),
            str(r.get("course_code") or ""),
        )
    )

    # ----------------------------- Profile / department ------------------- #
    profile = _student_profile_overview(student_id)
    department_id = profile.get("department_id")
    faculty_id = profile.get("faculty_id")
    department_name = profile.get("department_name")
    major = profile.get("major") or department_name

    # Pull avg grade per enrolled course — drives the recommendation engine.
    enrolled_id_set = {int(r["id"]) for r in enrolled_rows if r.get("id") is not None}
    submission_rows, assignment_meta = _student_submission_meta(
        student_id, course_ids=enrolled_id_set,
    )
    avg_grade_per_course: dict[int, float] = {
        cid: _course_avg_grade_10(
            course_id=cid,
            submission_rows=submission_rows,
            assignment_meta=assignment_meta,
        )
        for cid in enrolled_id_set
    }

    # ----------------------------- Catalog -------------------------------- #
    catalog = _load_path_catalog(
        department_id=department_id,
        faculty_id=faculty_id,
        exclude_ids=enrolled_id_set,
    )
    catalog_sorted = sorted(
        catalog,
        key=lambda r: (
            str(r.get("academic_year") or ""),
            int(r.get("semester") or 0),
            str(r.get("course_code") or ""),
        ),
    )

    # Resolve department names for the next-step "reason" copy.
    referenced_dept_ids = {
        int(d) for r in catalog_sorted + enrolled_rows
        if (d := r.get("from_department")) is not None
    }
    dept_name_by_id: dict[int, str] = {}
    if referenced_dept_ids:
        try:
            dep_rows = (
                sb.table("departments")
                .select("id, name")
                .in_("id", sorted(referenced_dept_ids))
                .execute()
                .data
                or []
            )
            dept_name_by_id = {
                int(d["id"]): str(d.get("name") or "")
                for d in dep_rows
                if d.get("id") is not None
            }
        except Exception:
            dept_name_by_id = {}

    today = datetime.now(timezone.utc).date()

    # ----------------------------- Build path entries --------------------- #
    # We track two independent dedup sets — a catalog course can be both
    # an alternative for course A and a recommendation suggestion for
    # course B; they live in different UI sections so the duplication
    # isn't confusing. ``alt_used_ids`` keeps swap suggestions distinct
    # across path entries, ``rec_used_ids`` does the same for the
    # remedial / accelerated suggestion lists.
    alt_used_ids: set[int] = set()
    rec_used_ids: set[int] = set()
    path_courses: list[StudentPathCourse] = []

    for row in enrolled_rows:
        cid = int(row["id"])
        status = _path_course_status(row, today=today)
        avg_g = avg_grade_per_course.get(cid)
        recommendation = _build_path_recommendation(
            course=row,
            status=status,
            avg_grade_10=avg_g if avg_g and avg_g > 0 else None,
            catalog=catalog_sorted,
            used_ids=rec_used_ids,
        )
        path_courses.append(
            StudentPathCourse(
                id=f"enrolled-{cid}",
                course_id=cid,
                code=str(row.get("course_code") or f"C{cid}"),
                name=str(row.get("title") or "Untitled course"),
                status=status,
                semester=row.get("semester"),
                academic_year=row.get("academic_year"),
                alternatives=_pick_path_alternatives(
                    row, catalog_sorted, used_ids=alt_used_ids,
                ),
                recommendation=recommendation,
            )
        )

    # ----------------------------- Next-step recommendations -------------- #
    # Surface 2-4 catalog courses as forward-looking ideas. Same-department
    # rows come first; cross-department picks are framed as "broaden
    # beyond your major". We exclude both the alternatives and the
    # recommendation suggestions to avoid showing the same course in two
    # different sections.
    if department_id is not None:
        dept_first = sorted(
            catalog_sorted,
            key=lambda r: (0 if r.get("from_department") == department_id else 1),
        )
    else:
        dept_first = catalog_sorted
    consumed = alt_used_ids | rec_used_ids
    next_step_pool = [
        c for c in dept_first if int(c.get("id") or 0) not in consumed
    ]
    target_next = 0
    if next_step_pool:
        target_next = min(4, max(2, len(next_step_pool) // 2))
        target_next = min(target_next, len(next_step_pool))

    next_steps: list[StudentPathNextStep] = []
    for cand in next_step_pool[:target_next]:
        cid = int(cand["id"])
        cand_dept_id = cand.get("from_department")
        cand_dept_name = (
            dept_name_by_id.get(int(cand_dept_id)) if cand_dept_id is not None else None
        )
        sem = cand.get("semester")
        if cand_dept_name and department_name and cand_dept_name == department_name:
            if sem is not None:
                reason = f"Catalog course in {cand_dept_name} (semester {sem}) — fits your program."
            else:
                reason = f"Catalog course in {cand_dept_name} — fits your program."
        elif cand_dept_name:
            if sem is not None:
                reason = f"Open elective from {cand_dept_name} (semester {sem}) — broaden beyond your major."
            else:
                reason = f"Open elective from {cand_dept_name} — broaden beyond your major."
        else:
            reason = "Open catalog course — review with your advisor."
        next_steps.append(
            StudentPathNextStep(
                course_id=cid,
                code=str(cand.get("course_code") or f"C{cid}"),
                name=str(cand.get("title") or "Untitled course"),
                semester=sem,
                academic_year=cand.get("academic_year"),
                reason=reason,
            )
        )

    progress_pct = _path_progress_pct(path_courses)
    header = StudentPathHeader(
        name=(
            f"{major} pathway"
            if major
            else "Personalised learning pathway"
        ),
        description=_path_header_description(
            courses=path_courses,
            next_steps=next_steps,
            progress_pct=progress_pct,
            major=major,
        ),
        major=major,
        progress=progress_pct,
    )

    enrolled_summary = [
        StudentCourseSummary(
            id=int(r["id"]),
            course_code=r.get("course_code"),
            title=str(r.get("title") or ""),
            semester=r.get("semester"),
            academic_year=r.get("academic_year"),
            class_room=r.get("class_room"),
        )
        for r in enrolled_rows
        if r.get("id") is not None
    ]

    if path_courses:
        data_source = "real"
    elif next_steps:
        data_source = "catalog_only"
    else:
        data_source = "empty"

    return StudentLearningPathResponse(
        student_id=student_id,
        path=header,
        courses=path_courses,
        next_steps=next_steps,
        enrolled_courses=enrolled_summary,
        generated_at_utc=datetime.now(timezone.utc),
        data_source=data_source,
    )


@router.get("/{student_id}/competency", response_model=CompetencyResponse)
async def get_competency_analysis(
    db: AiDbDep,
    request: Request,
    student_id: str,
    since_weeks: int = Query(default=8, ge=2, le=30),
    course_id: int | None = Query(default=None, ge=1),
):
    """Preliminary strengths/weaknesses using weekly assignment + activity features."""
    rows = await _load_weekly_feature_rows(db, student_id, since_weeks=since_weeks)
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    allowed_course_ids, role_name = _authorized_course_scope(user)
    if role_name == "Student" and user.get("user_id") != student_id:
        raise HTTPException(status_code=403, detail="Students can only view own analytics")
    if course_id is not None:
        rows = [r for r in rows if r.get("course_id") == course_id]
    if allowed_course_ids is not None:
        rows = [r for r in rows if r.get("course_id") in allowed_course_ids]
    if not rows:
        raise HTTPException(status_code=404, detail=f"No weekly features found for student_id={student_id}")

    dims = _score_competency(rows)
    strengths = [d.name for d in dims if d.score >= 0.65]
    weaknesses = [d.name for d in dims if d.score < 0.45]
    summary = (
        "Strong overall trajectory with consistent habits."
        if not weaknesses
        else f"Needs support in: {', '.join(weaknesses)}."
    )
    return CompetencyResponse(
        student_id=student_id,
        weeks_used=min(since_weeks, len(rows)),
        dimensions=dims,
        strengths=strengths,
        weaknesses=weaknesses,
        summary=summary,
    )


@router.get("/{student_id}/learning-path", response_model=LearningPathResponse)
async def get_learning_path(
    db: AiDbDep,
    request: Request,
    student_id: str,
    since_weeks: int = Query(default=8, ge=2, le=30),
    course_id: int | None = Query(default=None, ge=1),
):
    """Generate simple dynamic study actions based on competency dimensions."""
    rows = await _load_weekly_feature_rows(db, student_id, since_weeks=since_weeks)
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    allowed_course_ids, role_name = _authorized_course_scope(user)
    if role_name == "Student" and user.get("user_id") != student_id:
        raise HTTPException(status_code=403, detail="Students can only view own analytics")
    if course_id is not None:
        rows = [r for r in rows if r.get("course_id") == course_id]
    if allowed_course_ids is not None:
        rows = [r for r in rows if r.get("course_id") in allowed_course_ids]
    if not rows:
        raise HTTPException(status_code=404, detail=f"No weekly features found for student_id={student_id}")

    dims = _score_competency(rows)
    score_map = {d.name: d.score for d in dims}
    focus_areas: list[str] = []
    actions: list[str] = []

    if score_map["academic_performance"] < 0.55:
        focus_areas.append("assignment mastery")
        actions.append("Review weak quiz/assignment topics and do 2 targeted practice sessions per week.")
    if score_map["engagement"] < 0.55:
        focus_areas.append("platform engagement")
        actions.append("Schedule fixed daily LMS check-ins (20-30 minutes) and track completion.")
    if score_map["attendance"] < 0.60:
        focus_areas.append("attendance recovery")
        actions.append("Set attendance reminders and contact advisor after 2 consecutive absences.")
    if score_map["consistency"] < 0.60:
        focus_areas.append("study consistency")
        actions.append("Break tasks into smaller weekly milestones to reduce inactivity streak.")

    if not actions:
        focus_areas.append("growth acceleration")
        actions.append("Take one advanced elective module and start peer-mentoring sessions.")

    rationale = (
        "Recommendations are based on recent weekly behavior, attendance, and assessment outcomes."
    )
    return LearningPathResponse(
        student_id=student_id,
        weeks_used=min(since_weeks, len(rows)),
        focus_areas=focus_areas,
        recommended_actions=actions,
        rationale=rationale,
    )


@router.get("/{student_id}/dropout-risk", response_model=DropoutRiskResponse)
async def get_dropout_risk(
    db: AiDbDep,
    request: Request,
    student_id: str,
    course_id: int | None = Query(default=None, ge=1),
):
    """Predict low/medium/high risk from the latest weekly feature snapshot."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    allowed_course_ids, role_name = _authorized_course_scope(user)
    if role_name == "Student" and user.get("user_id") != student_id:
        raise HTTPException(status_code=403, detail="Students can only view own analytics")

    q: dict[str, Any] = {"user_id": student_id}
    if course_id is not None:
        if allowed_course_ids is not None and course_id not in allowed_course_ids:
            raise HTTPException(status_code=403, detail="Forbidden course scope")
        q["course_id"] = course_id
    elif allowed_course_ids is not None:
        if not allowed_course_ids:
            raise HTTPException(status_code=404, detail=f"No scoped course data for student_id={student_id}")
        q["course_id"] = {"$in": sorted(allowed_course_ids)}

    row = await db["student_weekly_features"].find_one(
        q,
        sort=[("week_start", -1)],
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"No weekly features found for student_id={student_id}")
    if not _MODEL_PATH.exists():
        raise HTTPException(status_code=503, detail=f"Model not found at {_MODEL_PATH}")

    features = dict(row.get("features") or {})
    score, risk_level = _predict_risk_from_features(features)

    top_factors = sorted(
        [{"feature": col, "value": float(features.get(col) or 0.0)} for col in FEATURE_COLUMNS],
        key=lambda item: abs(item["value"]),
        reverse=True,
    )[:5]

    return DropoutRiskResponse(
        student_id=student_id,
        week_start=row.get("week_start"),
        risk_level=risk_level,
        risk_score=round(float(score), 4),
        top_factors=top_factors,
        model_path=str(_MODEL_PATH),
        data_source="REAL_MONGO",
    )
