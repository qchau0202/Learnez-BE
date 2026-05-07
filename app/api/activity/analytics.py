"""Analytics - Competency, Learning Path, Dropout Risk.

This module intentionally keeps scoring logic transparent so lecturers can inspect
why a student got a given recommendation or risk level.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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


def _load_enrollment_pairs(allowed_course_ids: set[int] | None) -> set[tuple[str, int]] | None:
    """Return the authoritative ``(student_id, course_id)`` enrollment set.

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


def _load_course_meta(course_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Pull ``course_code``, ``title``, ``class_room`` for the given course ids."""
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


def _resolve_student_org_map(user_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Look up faculty/department/identity for a batch of student user_ids.

    Resolution rules:
      - Prefer ``student_profiles`` (department_id / faculty_id, plus student_id, class).
      - Faculty is resolved via ``departments.from_faculty`` when the profile's
        ``faculty_id`` is null but a department is set.
      - Falls back to ``users`` for full_name/email so admins can see real names.
    Returns a dict keyed by user_id.
    """
    if not user_ids:
        return {}
    sb = get_supabase(service_role=True)
    if not sb:
        return {}
    uids = sorted({str(u).strip() for u in user_ids if str(u or "").strip()})
    if not uids:
        return {}
    fac_rows = sb.table("faculties").select("id, name").execute().data or []
    faculty_name_by_id: dict[int, str] = {
        int(r["id"]): str(r.get("name") or "") for r in fac_rows if r.get("id") is not None
    }
    dep_rows = sb.table("departments").select("id, name, from_faculty").execute().data or []
    dep_by_id: dict[int, dict[str, Any]] = {
        int(r["id"]): r for r in dep_rows if r.get("id") is not None
    }

    # PostgREST encodes ``in.()`` filters in the URL; with several hundred
    # UUIDs the URL exceeds the gateway limit and Supabase returns a plain
    # ``Bad Request``. Chunk the lookups so each request stays small.
    def _chunked_in(table: str, columns: str, ids: list[str], chunk_size: int = 100) -> list[dict[str, Any]]:
        out_rows: list[dict[str, Any]] = []
        for i in range(0, len(ids), chunk_size):
            batch = ids[i : i + chunk_size]
            res = sb.table(table).select(columns).in_("user_id", batch).execute()
            out_rows.extend(res.data or [])
        return out_rows

    sp_rows = _chunked_in(
        "student_profiles",
        "user_id, student_id, class, faculty_id, department_id",
        uids,
    )
    user_rows = _chunked_in(
        "users",
        "user_id, full_name, email",
        uids,
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
    return out


def _annotate_cards_with_org(cards: list[StudentRiskCard]) -> list[StudentRiskCard]:
    if not cards:
        return cards
    org = _resolve_student_org_map([c.student_id for c in cards])
    course_ids = sorted({int(c.course_id) for c in cards if isinstance(c.course_id, int)})
    course_meta = _load_course_meta(course_ids)
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

    cards = _annotate_cards_with_org(cards)

    if only_real_enrollments:
        enrollment_pairs = _load_enrollment_pairs(allowed_course_ids)
        cards = _filter_cards_to_real_students(cards, enrollment_pairs=enrollment_pairs)

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
