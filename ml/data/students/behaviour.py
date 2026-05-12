"""Layer behaviour + risk side-tables on top of an existing student seed.

``students.student1`` produces the canonical academic data (weekly
features, attendance, submissions). This module fills the *analytic*
collections needed by the Behavior / Risk Analysis tabs and the audit
pipeline:

* ``learnez_ai.risk_scores`` — backfilled risk history so lecturer
  at-risk dashboards see the student before they open their own tab;
* ``learnez_ai.competency_profiles`` — one row per subject area;
* ``elearning_raw.{activity,content,attendance,assessment}_events`` —
  compact synthetic raw events mirroring the rolled-up weekly features.

Every write is keyed on a deterministic compound key, so re-running is
a no-op past the first invocation. Run via the orchestrator
(``python -m ml.data.students.student1``) or stand-alone:

    python -m ml.data.students.behaviour
    python -m ml.data.students.behaviour --since-weeks 16 --skip-events --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.core.database import get_mongo_ai_db, get_mongo_raw_db, get_supabase  # noqa: E402


STUDENT_EMAIL_DEFAULT = "student1@email.com"

# Same model identifier the live endpoint uses so cached rows look
# identical regardless of which path produced them.
RISK_MODEL_VERSION = "rf_composite_v1"


def _idempotent_key(*parts: Any) -> str:
    """Stable SHA-1 used as idempotency_key on raw event collections."""
    payload = "::".join(str(p) for p in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _seeded_rng_for(*parts: Any):
    """Per-document deterministic RNG (kept separate from ``features._seeded_rng`` to dodge a cycle)."""
    import random

    digest = hashlib.sha1("::".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _svc():
    sb = get_supabase(service_role=True)
    if sb is None:
        raise SystemExit(
            "Missing Supabase service-role configuration. "
            "Export SUPABASE_SERVICE_ROLE_KEY before running."
        )
    return sb


def _resolve_student_id(sb, email: str) -> str:
    rows = (
        sb.table("users")
        .select("user_id, role_id")
        .ilike("email", email)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        raise SystemExit(f"User {email} not found in Supabase.")
    if rows[0].get("role_id") != 3:
        raise SystemExit(
            f"{email} is not a Student (role_id={rows[0].get('role_id')}); refusing to seed."
        )
    return str(rows[0]["user_id"])


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _date_floor(d: datetime) -> datetime:
    d = _utc(d)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


async def _load_weekly_features(
    ai_db, *, user_id: str, since_weeks: int,
) -> list[dict[str, Any]]:
    """Pull the seeded weekly feature snapshots in chronological order."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(weeks=since_weeks + 1)
    cursor = (
        ai_db["student_weekly_features"]
        .find({"user_id": user_id, "week_start": {"$gte": start, "$lt": end}})
        .sort([("week_start", 1)])
    )
    return await cursor.to_list(length=since_weeks * 60)


# ---------- Risk history backfill ---------- #


def _predict_risk_for_features(features: dict[str, Any]) -> tuple[float, str] | None:
    """Run the production risk model on one feature vector.

    Returns ``None`` if the model artefact is missing (CI / minimal
    images) — caller falls back to ``_heuristic_risk`` so the seed
    always produces something sensible.
    """
    try:
        from app.api.activity.analytics import _MODEL_PATH, _load_model  # noqa: WPS437
        from ml.training.dataset_builder import FEATURE_COLUMNS
        from ml.training.risk_bands import (
            load_thresholds,
            risk_level_from_score,
            score_from_probabilities,
        )
    except Exception:  # noqa: BLE001
        return None

    if not _MODEL_PATH.exists():
        return None

    try:
        model = _load_model()
        vector = [[float(features.get(col) or 0.0) for col in FEATURE_COLUMNS]]
        proba_by_class: dict[int, float] = {}
        if hasattr(model, "predict_proba"):
            probs = model.predict_proba(vector)[0]
            classes = [int(x) for x in model.classes_.tolist()]
            proba_by_class = {classes[i]: float(probs[i]) for i in range(len(classes))}
        score = score_from_probabilities(proba_by_class)
        thresholds_path = _MODEL_PATH.with_name("dropout_thresholds_composite.json")
        low_max, med_max = load_thresholds(thresholds_path)
        level = risk_level_from_score(score, low_max, med_max)
        return score, level
    except Exception:  # noqa: BLE001
        return None


def _heuristic_risk(features: dict[str, Any]) -> tuple[float, str]:
    """Cheap fallback: low attendance + low scores + low engagement → high risk."""
    attendance = float(features.get("attendance_rate") or 0.0)
    avg_score = float(features.get("avg_score_30d") or 0.0)  # 0..10
    logins = float(features.get("logins") or 0.0)
    late = float(features.get("submissions_late") or 0.0)
    inactivity = float(features.get("inactivity_streak_days") or 0.0)

    # Coefficients picked so ``at_risk`` lands 0.55-0.85 and ``thriving`` < 0.25.
    score = (
        0.35 * (1.0 - attendance)
        + 0.30 * (1.0 - max(0.0, min(1.0, avg_score / 10.0)))
        + 0.15 * max(0.0, 1.0 - min(1.0, logins / 10.0))
        + 0.10 * min(1.0, late / 4.0)
        + 0.10 * min(1.0, inactivity / 7.0)
    )
    score = max(0.05, min(0.95, score))
    if score <= 0.40:
        level = "low"
    elif score <= 0.70:
        level = "medium"
    else:
        level = "high"
    return score, level


async def _backfill_risk_scores(
    ai_db, *, user_id: str, weekly_rows: list[dict[str, Any]], dry_run: bool,
) -> int:
    """Upsert one ``risk_scores`` doc per weekly feature row."""
    if not weekly_rows:
        return 0

    from pymongo import UpdateOne

    ops: list[UpdateOne] = []
    fallback_used = 0
    model_used = 0
    for row in weekly_rows:
        features = row.get("features") or {}
        course_id = row.get("course_id")
        if course_id is None:
            continue
        course_id = int(course_id)

        week_start = row.get("week_start")
        if not isinstance(week_start, datetime):
            continue
        week_start = _utc(week_start)
        # Anchor ``computed_at`` to end-of-week: risk is produced once the week's data is in.
        computed_at = week_start + timedelta(days=6, hours=18)
        computed_day = _date_floor(computed_at)

        rs = _predict_risk_for_features(features)
        if rs is None:
            rs = _heuristic_risk(features)
            fallback_used += 1
            source_tag = "heuristic_seed"
        else:
            model_used += 1
            source_tag = "model_seed"
        score, level = rs

        doc = {
            "user_id": user_id,
            "course_id": course_id,
            "computed_at": computed_at,
            "predicted_at": computed_at,
            "computed_day": computed_day,
            "week_start": week_start,
            "risk_level": level,
            "risk_score": round(float(score), 4),
            "model_version": RISK_MODEL_VERSION,
            "source": source_tag,
            "metrics": {
                "avg_score_30d": float(features.get("avg_score_30d") or 0.0),
                "attendance_rate": float(features.get("attendance_rate") or 0.0),
                "inactivity_streak_days": int(features.get("inactivity_streak_days") or 0),
                "submissions_total": int(features.get("submissions_total") or 0),
                "submissions_late": int(features.get("submissions_late") or 0),
                "active_minutes": float(features.get("active_minutes") or 0.0),
                "logins": int(features.get("logins") or 0),
            },
            "summary": (
                "Seeded risk row backfilled from weekly features — "
                "regenerated by ``students.behaviour``."
            ),
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc),
        }
        ops.append(
            UpdateOne(
                {
                    "user_id": user_id,
                    "course_id": course_id,
                    "computed_day": computed_day,
                    "model_version": RISK_MODEL_VERSION,
                },
                {"$set": doc, "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        )

    if dry_run or not ops:
        print(
            f"    risk_scores : would upsert {len(ops)} rows "
            f"(model={model_used}, heuristic={fallback_used})"
        )
        return 0
    result = await ai_db["risk_scores"].bulk_write(ops, ordered=False)
    written = (result.upserted_count or 0) + (result.modified_count or 0)
    print(
        f"    risk_scores : upserted {written}/{len(ops)} "
        f"(model={model_used}, heuristic={fallback_used})"
    )
    return written


# ---------- Raw event lake ---------- #


def _spread_events_across_week(
    week_start: datetime,
    total_count: int,
    *,
    rng,
    weekdays: list[int] | None = None,
    hour_range: tuple[int, int] = (8, 22),
) -> list[datetime]:
    """Pick ``total_count`` plausible event timestamps within one week."""
    if total_count <= 0:
        return []
    if weekdays is None:
        weekdays = [0, 1, 2, 3, 4]
    out: list[datetime] = []
    for _ in range(total_count):
        weekday = rng.choice(weekdays)
        hour = rng.randint(hour_range[0], hour_range[1] - 1)
        minute = rng.randint(0, 59)
        out.append(week_start + timedelta(days=weekday, hours=hour, minutes=minute))
    out.sort()
    return out


async def _seed_activity_events(
    raw_db, *, user_id: str, weekly_rows: list[dict[str, Any]], dry_run: bool,
) -> int:
    """One ``login`` + one ``session_heartbeat`` per active day.

    Compressed to one login per active day (not per recorded ``logins``)
    because the audit pipeline only checks for presence.
    """
    from pymongo import UpdateOne

    ops: list[UpdateOne] = []
    now = datetime.now(timezone.utc)
    for row in weekly_rows:
        features = row.get("features") or {}
        course_id = row.get("course_id")
        week_start = row.get("week_start")
        if course_id is None or not isinstance(week_start, datetime):
            continue
        course_id = int(course_id)
        week_start = _utc(week_start)
        active_min = float(features.get("active_minutes") or 0.0)
        logins = int(features.get("logins") or 0)
        if logins <= 0 and active_min <= 1.0:
            continue
        rng = _seeded_rng_for("activity", user_id, course_id, week_start.isoformat())
        active_days = min(7, max(1, logins or 1))
        login_times = _spread_events_across_week(
            week_start, active_days, rng=rng, hour_range=(8, 21)
        )
        for ts in login_times:
            duration = max(15, int(active_min / max(active_days, 1) * 60))
            evt_id = _idempotent_key("login", user_id, course_id, ts.isoformat())
            doc = {
                "event_id": evt_id,
                "idempotency_key": evt_id,
                "event_type": "login",
                "event_time": ts,
                "source": "job",
                "user_id": user_id,
                "course_id": course_id,
                "duration_sec": duration,
                "schema_version": 1,
                "properties": {"seed_origin": "student1_behaviour"},
                "created_at": now,
            }
            ops.append(
                UpdateOne(
                    {"idempotency_key": evt_id},
                    {"$set": doc, "$setOnInsert": {"first_seen_at": now}},
                    upsert=True,
                )
            )
            # Heartbeat 30 min after login — gives the aggregator a session-duration signal.
            hb_ts = ts + timedelta(minutes=30)
            hb_id = _idempotent_key("heartbeat", user_id, course_id, hb_ts.isoformat())
            ops.append(
                UpdateOne(
                    {"idempotency_key": hb_id},
                    {
                        "$set": {
                            **doc,
                            "event_id": hb_id,
                            "idempotency_key": hb_id,
                            "event_type": "session_heartbeat",
                            "event_time": hb_ts,
                            "duration_sec": 1800,
                        },
                        "$setOnInsert": {"first_seen_at": now},
                    },
                    upsert=True,
                )
            )

    if dry_run or not ops:
        print(f"    activity_events : would upsert {len(ops)} events")
        return 0
    await raw_db["activity_events"].create_index("idempotency_key", unique=True)
    result = await raw_db["activity_events"].bulk_write(ops, ordered=False)
    written = (result.upserted_count or 0) + (result.modified_count or 0)
    print(f"    activity_events : upserted {written}/{len(ops)} events")
    return written


async def _seed_content_events(
    raw_db, *, user_id: str, weekly_rows: list[dict[str, Any]], dry_run: bool,
) -> int:
    """One ``material_open`` event per ``materials_viewed`` count."""
    from pymongo import UpdateOne

    ops: list[UpdateOne] = []
    now = datetime.now(timezone.utc)
    for row in weekly_rows:
        features = row.get("features") or {}
        course_id = row.get("course_id")
        week_start = row.get("week_start")
        if course_id is None or not isinstance(week_start, datetime):
            continue
        course_id = int(course_id)
        week_start = _utc(week_start)
        materials = int(features.get("materials_viewed") or 0)
        open_time = float(features.get("material_open_time_sec") or 0.0)
        if materials <= 0:
            continue
        rng = _seeded_rng_for("content", user_id, course_id, week_start.isoformat())
        timestamps = _spread_events_across_week(week_start, materials, rng=rng)
        per_open_sec = max(60, int(open_time / max(materials, 1)))
        for ts in timestamps:
            evt_id = _idempotent_key("material_open", user_id, course_id, ts.isoformat())
            ops.append(
                UpdateOne(
                    {"idempotency_key": evt_id},
                    {
                        "$set": {
                            "event_id": evt_id,
                            "idempotency_key": evt_id,
                            "event_type": "material_open",
                            "event_time": ts,
                            "source": "job",
                            "user_id": user_id,
                            "course_id": course_id,
                            "duration_sec": per_open_sec,
                            "schema_version": 1,
                            "properties": {"seed_origin": "student1_behaviour"},
                            "created_at": now,
                        },
                        "$setOnInsert": {"first_seen_at": now},
                    },
                    upsert=True,
                )
            )

    if dry_run or not ops:
        print(f"    content_events  : would upsert {len(ops)} events")
        return 0
    await raw_db["content_events"].create_index("idempotency_key", unique=True)
    result = await raw_db["content_events"].bulk_write(ops, ordered=False)
    written = (result.upserted_count or 0) + (result.modified_count or 0)
    print(f"    content_events  : upserted {written}/{len(ops)} events")
    return written


async def _seed_attendance_events(
    sb, raw_db, *, user_id: str, course_ids: list[int], dry_run: bool,
) -> int:
    """Mirror every ``course_attendance`` row into ``attendance_events``."""
    if not course_ids:
        return 0

    rows = (
        sb.table("course_attendance")
        .select("student_id, course_id, session_date, status, marked_at")
        .eq("student_id", user_id)
        .in_("course_id", course_ids)
        .execute()
        .data
        or []
    )
    if not rows:
        print("    attendance_events: 0 rows in course_attendance, skipping")
        return 0

    from pymongo import UpdateOne

    ops: list[UpdateOne] = []
    now = datetime.now(timezone.utc)
    for r in rows:
        session_date = r.get("session_date")
        if not session_date:
            continue
        if isinstance(session_date, str):
            session_date = date.fromisoformat(session_date)
        elif isinstance(session_date, datetime):
            session_date = session_date.date()
        ts = datetime.combine(session_date, time(9, 0), tzinfo=timezone.utc)
        status = (r.get("status") or "").lower()
        event_type = "session_attended" if status in {"present", "late"} else "session_absent"
        course_id = int(r.get("course_id") or 0)
        evt_id = _idempotent_key("attendance", user_id, course_id, session_date.isoformat())
        ops.append(
            UpdateOne(
                {"idempotency_key": evt_id},
                {
                    "$set": {
                        "event_id": evt_id,
                        "idempotency_key": evt_id,
                        "event_type": event_type,
                        "event_time": ts,
                        "source": "job",
                        "user_id": user_id,
                        "course_id": course_id,
                        "status": status,
                        "schema_version": 1,
                        "properties": {"seed_origin": "student1_behaviour"},
                        "created_at": now,
                    },
                    "$setOnInsert": {"first_seen_at": now},
                },
                upsert=True,
            )
        )

    if dry_run or not ops:
        print(f"    attendance_events: would upsert {len(ops)} events")
        return 0
    await raw_db["attendance_events"].create_index("idempotency_key", unique=True)
    result = await raw_db["attendance_events"].bulk_write(ops, ordered=False)
    written = (result.upserted_count or 0) + (result.modified_count or 0)
    print(f"    attendance_events: upserted {written}/{len(ops)} events")
    return written


async def _seed_assessment_events(
    sb, raw_db, *, user_id: str, course_ids: list[int], dry_run: bool,
) -> int:
    """Mirror every assignment_submission into ``assessment_events``.

    Emits one ``submission_created`` + (when graded) one
    ``graded_finalized`` event per submission.
    """
    if not course_ids:
        return 0

    rows = (
        sb.table("assignment_submissions")
        .select(
            "id, student_id, assignment_id, final_score, status, "
            "submitted_at, graded_at, "
            "assignments!inner(id, module_id, modules!inner(course_id))"
        )
        .eq("student_id", user_id)
        .execute()
        .data
        or []
    )
    if not rows:
        print("    assessment_events: 0 submissions, skipping")
        return 0

    from pymongo import UpdateOne

    course_id_set = set(course_ids)
    ops: list[UpdateOne] = []
    now = datetime.now(timezone.utc)
    for r in rows:
        try:
            course_id = int(r["assignments"]["modules"]["course_id"])
        except Exception:  # noqa: BLE001
            continue
        if course_id not in course_id_set:
            continue
        submitted_at = r.get("submitted_at")
        if not submitted_at:
            continue
        if isinstance(submitted_at, str):
            try:
                submitted_at = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
            except ValueError:
                continue
        submitted_at = _utc(submitted_at)
        sub_id = int(r["id"])

        status = (r.get("status") or "").lower()
        timing_label: str | None
        if status == "late":
            timing_label = "late"
        elif status == "draft":
            timing_label = None
        else:
            timing_label = "on_time"

        create_id = _idempotent_key(
            "submission_created", user_id, sub_id, submitted_at.isoformat()
        )
        ops.append(
            UpdateOne(
                {"idempotency_key": create_id},
                {
                    "$set": {
                        "event_id": create_id,
                        "idempotency_key": create_id,
                        "event_type": "submission_created",
                        "event_time": submitted_at,
                        "source": "job",
                        "user_id": user_id,
                        "course_id": course_id,
                        "assignment_id": int(r.get("assignment_id") or 0),
                        "submission_id": sub_id,
                        "timing_label": timing_label,
                        "schema_version": 1,
                        "properties": {"seed_origin": "student1_behaviour"},
                        "created_at": now,
                    },
                    "$setOnInsert": {"first_seen_at": now},
                },
                upsert=True,
            )
        )

        # Graded? Always emit the matching ``graded_finalized`` regardless of status string.
        final_score = r.get("final_score")
        if final_score is None:
            continue
        graded_at = r.get("graded_at") or submitted_at + timedelta(days=2)
        if isinstance(graded_at, str):
            try:
                graded_at = datetime.fromisoformat(graded_at.replace("Z", "+00:00"))
            except ValueError:
                graded_at = submitted_at + timedelta(days=2)
        graded_at = _utc(graded_at)
        grade_id = _idempotent_key(
            "graded_finalized", user_id, sub_id, graded_at.isoformat()
        )
        ops.append(
            UpdateOne(
                {"idempotency_key": grade_id},
                {
                    "$set": {
                        "event_id": grade_id,
                        "idempotency_key": grade_id,
                        "event_type": "graded_finalized",
                        "event_time": graded_at,
                        "source": "job",
                        "user_id": user_id,
                        "course_id": course_id,
                        "assignment_id": int(r.get("assignment_id") or 0),
                        "submission_id": sub_id,
                        "final_score": float(final_score),
                        "timing_label": timing_label,
                        "schema_version": 1,
                        "properties": {"seed_origin": "student1_behaviour"},
                        "created_at": now,
                    },
                    "$setOnInsert": {"first_seen_at": now},
                },
                upsert=True,
            )
        )

    if dry_run or not ops:
        print(f"    assessment_events: would upsert {len(ops)} events")
        return 0
    await raw_db["assessment_events"].create_index("idempotency_key", unique=True)
    result = await raw_db["assessment_events"].bulk_write(ops, ordered=False)
    written = (result.upserted_count or 0) + (result.modified_count or 0)
    print(f"    assessment_events: upserted {written}/{len(ops)} events")
    return written


# ---------- Competency profile (strengths/weaknesses) ---------- #


def _competency_subject_code(course_code: str) -> str:
    """Coarse subject area from a TDTU course code (first 4 chars)."""
    return (course_code or "").upper()[:4] or "GEN"


async def _seed_competency_profiles(
    ai_db, sb, *,
    user_id: str,
    course_ids: list[int],
    weekly_rows: list[dict[str, Any]],
    dry_run: bool,
) -> int:
    """One competency_profile per (user, subject_code)."""
    if not course_ids:
        return 0

    courses = (
        sb.table("courses")
        .select("id, course_code, title")
        .in_("id", course_ids)
        .execute()
        .data
        or []
    )
    code_by_id = {int(c["id"]): (c.get("course_code") or "") for c in courses}
    title_by_id = {int(c["id"]): (c.get("title") or "") for c in courses}

    latest_per_course: dict[int, dict[str, Any]] = {}
    for row in weekly_rows:
        course_id = int(row.get("course_id") or 0)
        if course_id <= 0:
            continue
        ws = row.get("week_start")
        if not isinstance(ws, datetime):
            continue
        current = latest_per_course.get(course_id)
        if current is None or _utc(current["week_start"]) < _utc(ws):
            latest_per_course[course_id] = row

    grouped: dict[str, list[dict[str, Any]]] = {}
    for course_id, row in latest_per_course.items():
        subj = _competency_subject_code(code_by_id.get(course_id, ""))
        grouped.setdefault(subj, []).append(
            {
                "course_id": course_id,
                "course_code": code_by_id.get(course_id, ""),
                "title": title_by_id.get(course_id, ""),
                "features": row.get("features") or {},
            }
        )

    from pymongo import UpdateOne

    ops: list[UpdateOne] = []
    now = datetime.now(timezone.utc)
    for subj, entries in grouped.items():
        avg_score = sum(
            float(e["features"].get("avg_score_30d") or 0.0) for e in entries
        ) / max(len(entries), 1)
        attendance = sum(
            float(e["features"].get("attendance_rate") or 0.0) for e in entries
        ) / max(len(entries), 1)
        completion = sum(
            float(e["features"].get("submissions_on_time") or 0.0) for e in entries
        ) / max(
            sum(float(e["features"].get("submissions_total") or 0.0) for e in entries),
            1.0,
        )
        # Weighted mastery: 0.5 * score + 0.3 * attendance + 0.2 * on-time rate.
        mastery = round(
            0.50 * max(0.0, min(1.0, avg_score / 10.0))
            + 0.30 * max(0.0, min(1.0, attendance))
            + 0.20 * max(0.0, min(1.0, completion)),
            4,
        )
        if mastery >= 0.80:
            band = "strong"
        elif mastery >= 0.60:
            band = "developing"
        else:
            band = "needs_focus"

        doc = {
            "user_id": user_id,
            "subject_code": subj,
            "schema_version": 1,
            "mastery_score": mastery,
            "band": band,
            "metrics": {
                "avg_score_10pt": round(avg_score, 2),
                "attendance_rate": round(attendance, 4),
                "on_time_rate": round(completion, 4),
                "courses_included": [e["course_id"] for e in entries],
            },
            "course_summaries": [
                {
                    "course_id": e["course_id"],
                    "course_code": e["course_code"],
                    "title": e["title"],
                    "avg_score_10pt": round(
                        float(e["features"].get("avg_score_30d") or 0.0), 2
                    ),
                    "attendance_rate": round(
                        float(e["features"].get("attendance_rate") or 0.0), 4
                    ),
                }
                for e in entries
            ],
            "updated_at": now,
        }
        ops.append(
            UpdateOne(
                {"user_id": user_id, "subject_code": subj},
                {"$set": doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        )

    if dry_run or not ops:
        print(f"    competency_profiles: would upsert {len(ops)} profile(s)")
        return 0
    await ai_db["competency_profiles"].create_index(
        [("user_id", 1), ("subject_code", 1)], unique=True
    )
    result = await ai_db["competency_profiles"].bulk_write(ops, ordered=False)
    written = (result.upserted_count or 0) + (result.modified_count or 0)
    print(f"    competency_profiles: upserted {written}/{len(ops)} profile(s)")
    return written


# ---------- Orchestration ---------- #


async def seed_behaviour_and_risk(
    *,
    user_id: str,
    since_weeks: int = 16,
    skip_events: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """Run every behaviour + risk side-table seeder for one user.

    Returns per-collection upsert counts so the orchestrator can roll
    them into its summary line.
    """
    ai_db = get_mongo_ai_db()
    raw_db = get_mongo_raw_db()
    sb = _svc()

    weekly_rows = await _load_weekly_features(
        ai_db, user_id=user_id, since_weeks=since_weeks
    )
    course_ids = sorted({int(r["course_id"]) for r in weekly_rows if r.get("course_id")})
    print(
        f"  Loaded {len(weekly_rows)} weekly feature row(s) across "
        f"{len(course_ids)} course(s); since_weeks={since_weeks}"
    )

    counts: dict[str, int] = {}
    counts["risk_scores"] = await _backfill_risk_scores(
        ai_db, user_id=user_id, weekly_rows=weekly_rows, dry_run=dry_run
    )
    counts["competency_profiles"] = await _seed_competency_profiles(
        ai_db, sb,
        user_id=user_id,
        course_ids=course_ids,
        weekly_rows=weekly_rows,
        dry_run=dry_run,
    )

    if skip_events:
        print("  skip-events: not writing to elearning_raw.*")
        counts["activity_events"] = 0
        counts["content_events"] = 0
        counts["attendance_events"] = 0
        counts["assessment_events"] = 0
        return counts

    counts["activity_events"] = await _seed_activity_events(
        raw_db, user_id=user_id, weekly_rows=weekly_rows, dry_run=dry_run
    )
    counts["content_events"] = await _seed_content_events(
        raw_db, user_id=user_id, weekly_rows=weekly_rows, dry_run=dry_run
    )
    counts["attendance_events"] = await _seed_attendance_events(
        sb, raw_db, user_id=user_id, course_ids=course_ids, dry_run=dry_run
    )
    counts["assessment_events"] = await _seed_assessment_events(
        sb, raw_db, user_id=user_id, course_ids=course_ids, dry_run=dry_run
    )
    return counts


async def _run(args: argparse.Namespace) -> int:
    sb = _svc()
    user_id = _resolve_student_id(sb, args.email)
    print("=" * 72)
    print(f"Seeding behaviour + risk side-tables for {args.email} ({user_id})")
    print(f"  since_weeks   = {args.since_weeks}")
    print(f"  skip_events   = {args.skip_events}")
    print(f"  dry_run       = {args.dry_run}")
    print("=" * 72)

    counts = await seed_behaviour_and_risk(
        user_id=user_id,
        since_weeks=args.since_weeks,
        skip_events=args.skip_events,
        dry_run=args.dry_run,
    )

    print("-" * 72)
    total = sum(counts.values())
    detail = "  ".join(f"{k}={v}" for k, v in counts.items())
    print(f"total docs written : {total}")
    print(f"  {detail}")
    if args.dry_run:
        print("dry-run: nothing was actually persisted.")
    print("=" * 72)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Layer behaviour + risk analytic side-tables onto an existing "
            "demo seed for one student."
        ),
    )
    p.add_argument("--email", default=STUDENT_EMAIL_DEFAULT,
                   help=f"Demo account email (default {STUDENT_EMAIL_DEFAULT!r}).")
    p.add_argument("--since-weeks", type=int, default=16,
                   help="Look-back window when loading weekly features (default 16).")
    p.add_argument("--skip-events", action="store_true",
                   help="Only refresh risk_scores + competency_profiles; skip elearning_raw writes.")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve what would happen, but skip the Mongo writes.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
