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

from app.api.deps import DbDep
from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP
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
    week_start: datetime | None = None
    risk_level: str
    risk_score: float = Field(ge=0.0, le=1.0)
    avg_score_30d: float
    attendance_rate: float
    inactivity_streak_days: int
    summary: str


class AnalyticsOverviewResponse(BaseModel):
    students_considered: int
    risk_distribution: dict[str, int]
    avg_risk_score: float
    avg_attendance_rate: float
    avg_score_30d: float
    generated_at_utc: datetime
    data_source: str


@lru_cache(maxsize=1)
def _load_model():
    return joblib_load(_MODEL_PATH)


async def _load_weekly_feature_rows(db: DbDep, student_id: str, *, since_weeks: int) -> list[dict[str, Any]]:
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
    db: DbDep,
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
    latest_by_user: dict[str, dict[str, Any]] = {}
    for d in docs:
        uid = str(d.get("user_id") or "").strip()
        if uid and uid not in latest_by_user:
            latest_by_user[uid] = d
            if len(latest_by_user) >= max_users:
                break
    return list(latest_by_user.values())


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


@router.get("/overview", response_model=AnalyticsOverviewResponse)
async def get_analytics_overview(
    db: DbDep,
    request: Request,
    since_weeks: int = Query(default=8, ge=2, le=30),
    max_users: int = Query(default=1000, ge=20, le=5000),
    course_id: int | None = Query(default=None, ge=1),
):
    """Dashboard summary for current student risk and key learning indicators."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    allowed_course_ids, _ = _authorized_course_scope(user)
    if course_id is not None and allowed_course_ids is not None and course_id not in allowed_course_ids:
        raise HTTPException(status_code=403, detail="Forbidden course scope")
    rows = await _load_latest_rows_for_many_users(
        db,
        since_weeks=since_weeks,
        max_users=max_users,
        allowed_course_ids=allowed_course_ids,
        course_id=course_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No student_weekly_features rows found for requested window.")

    risk_dist = {"low": 0, "medium": 0, "high": 0}
    scores: list[float] = []
    attendance_vals: list[float] = []
    grade_vals: list[float] = []

    for row in rows:
        feats = dict(row.get("features") or {})
        score, level = _predict_risk_from_features(feats)
        risk_dist[level] = risk_dist.get(level, 0) + 1
        scores.append(score)
        attendance_vals.append(float(feats.get("attendance_rate") or 0.0))
        grade_vals.append(float(feats.get("avg_score_30d") or 0.0))

    return AnalyticsOverviewResponse(
        students_considered=len(rows),
        risk_distribution=risk_dist,
        avg_risk_score=round(float(mean(scores)), 4),
        avg_attendance_rate=round(float(mean(attendance_vals)), 4),
        avg_score_30d=round(float(mean(grade_vals)), 4),
        generated_at_utc=datetime.now(timezone.utc),
        data_source="REAL_MONGO",
    )


@router.get("/students", response_model=list[StudentRiskCard])
async def list_student_risk_cards(
    db: DbDep,
    request: Request,
    since_weeks: int = Query(default=8, ge=2, le=30),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    course_id: int | None = Query(default=None, ge=1),
):
    """Student cards for dashboards: risk level, score, and plain-language summary."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    allowed_course_ids, _ = _authorized_course_scope(user)
    if course_id is not None and allowed_course_ids is not None and course_id not in allowed_course_ids:
        raise HTTPException(status_code=403, detail="Forbidden course scope")
    rows = await _load_latest_rows_for_many_users(
        db,
        since_weeks=since_weeks,
        max_users=offset + limit + 50,
        allowed_course_ids=allowed_course_ids,
        course_id=course_id,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No student_weekly_features rows found for requested window.")

    selected = rows[offset : offset + limit]
    out: list[StudentRiskCard] = []
    for row in selected:
        uid = str(row.get("user_id") or "")
        feats = dict(row.get("features") or {})
        score, level = _predict_risk_from_features(feats)
        out.append(
            StudentRiskCard(
                student_id=uid,
                course_id=row.get("course_id"),
                week_start=row.get("week_start"),
                risk_level=level,
                risk_score=round(float(score), 4),
                avg_score_30d=float(feats.get("avg_score_30d") or 0.0),
                attendance_rate=float(feats.get("attendance_rate") or 0.0),
                inactivity_streak_days=int(feats.get("inactivity_streak_days") or 0),
                summary=_student_summary(feats, level),
            )
        )
    return out


@router.get("/{student_id}/competency", response_model=CompetencyResponse)
async def get_competency_analysis(
    db: DbDep,
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
    db: DbDep,
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
    db: DbDep,
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
