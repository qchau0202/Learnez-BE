#!/usr/bin/env python3
"""Demo seeder for the empty ``learnez_ai`` decision-layer collections.

Some collections in the AI database are intentionally **live-computed**
(``competency_profiles``, ``learning_paths``) or **reserved for future
jobs** (``student_daily_features``, ``course_engagement_features``,
``recommendation_explanations``). They show up empty in MongoDB Compass
because no production code path writes to them yet.

This script populates them with **realistic-shaped demo rows** so the
schema can be inspected — the rows are *derived* from data that already
exists (``student_weekly_features``, ``risk_scores``, the Supabase
courses table) so the relationships look real.

Safety / reversibility
----------------------
* Every seeded document has ``source: "demo_seed"`` and
  ``schema_version: 1``. To remove them later:

  .. code-block:: javascript

     db.student_daily_features.deleteMany({ source: "demo_seed" })
     db.course_engagement_features.deleteMany({ source: "demo_seed" })
     db.competency_profiles.deleteMany({ source: "demo_seed" })
     db.learning_paths.deleteMany({ source: "demo_seed" })
     db.recommendation_explanations.deleteMany({ source: "demo_seed" })

* Writes use ``replace_one(..., upsert=True)`` keyed on the natural key
  defined in :mod:`ml.data.mongodb_bootstrap`, so re-running the seeder
  refreshes existing rows instead of duplicating them.
* **No production code reads these collections today**, so seeding
  them does not change any UI or API behaviour. They exist purely so
  the schema is visible.

Usage::

    cd BE
    venv/bin/python -m ml.data.seed_ai_decision_layer
    venv/bin/python -m ml.data.seed_ai_decision_layer --dry-run
    venv/bin/python -m ml.data.seed_ai_decision_layer --max-users 50
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import random
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from app.core.database import get_mongo_ai_db, get_supabase  # noqa: E402

SEED_SOURCE = "demo_seed"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Shared lookups
# --------------------------------------------------------------------------- #


async def _load_weekly_features(
    db, max_users: int | None = None
) -> list[dict[str, Any]]:
    """Pull every weekly feature row, optionally capping users for speed."""
    cursor = db["student_weekly_features"].find(
        {}, {"_id": 0}
    ).sort([("week_start", 1)])
    rows: list[dict[str, Any]] = []
    seen_users: set[str] = set()
    async for doc in cursor:
        uid = str(doc.get("user_id") or "")
        if not uid:
            continue
        if max_users is not None and uid not in seen_users and len(seen_users) >= max_users:
            continue
        seen_users.add(uid)
        rows.append(doc)
    return rows


def _course_lookup_from_supabase() -> dict[int, dict[str, Any]]:
    """Map ``course_id -> {code, title, ...}`` for nicer demo content.

    Falls back to a synthesised name when Supabase is offline so the
    seeder still works in dev sandboxes.
    """
    sb = get_supabase(service_role=True)
    if not sb:
        return {}
    try:
        res = (
            sb.table("courses")
            .select("id, course_code, title, lecturer_id")
            .limit(2000)
            .execute()
        )
    except Exception:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in res.data or []:
        cid = row.get("id")
        if isinstance(cid, int):
            out[cid] = row
    return out


# --------------------------------------------------------------------------- #
# 1. student_daily_features
# --------------------------------------------------------------------------- #


def _split_weekly_to_daily(
    weekly: dict[str, Any], rng: random.Random
) -> list[dict[str, Any]]:
    """Distribute weekly aggregates across 7 days with mild jitter.

    The point isn't realism at the daily level (we don't have per-day
    truth from the simulator) — it's so the resulting documents have
    the right *shape* for whoever inspects the collection.
    """
    week_start = weekly.get("week_start")
    if not isinstance(week_start, datetime):
        return []
    features = weekly.get("features") or {}
    if not isinstance(features, dict):
        features = {}
    weights = [rng.random() + 0.5 for _ in range(7)]
    total_w = sum(weights) or 1.0
    weights = [w / total_w for w in weights]

    out: list[dict[str, Any]] = []
    for offset, weight in enumerate(weights):
        day = week_start + timedelta(days=offset)
        scaled = {
            "logins": int(round(float(features.get("logins") or 0) * weight)),
            "active_minutes": round(
                float(features.get("active_minutes") or 0.0) * weight, 2
            ),
            "materials_viewed": int(
                round(float(features.get("materials_viewed") or 0) * weight)
            ),
            "submissions_total": int(
                round(float(features.get("submissions_total") or 0) * weight)
            ),
            "assignments_due_7d": int(
                round(float(features.get("submissions_total") or 0) * weight) + 0
            ),
            "submissions_on_time_ratio_30d": _safe_ratio(
                features.get("submissions_on_time"),
                features.get("submissions_total"),
            ),
            "late_submission_count_30d": int(
                features.get("submissions_late") or 0
            ),
            "attendance_rate_30d": float(features.get("attendance_rate") or 0.0),
            "inactivity_streak_days": int(
                features.get("inactivity_streak_days") or 0
            ),
        }
        out.append(
            {
                "user_id": weekly.get("user_id"),
                "course_id": weekly.get("course_id"),
                "date": day.date().isoformat(),
                "features": scaled,
                "source": SEED_SOURCE,
                "source_event_max_time": weekly.get("source_event_max_time"),
                "schema_version": 1,
                "updated_at": _utc_now(),
            }
        )
    return out


def _safe_ratio(num: Any, denom: Any) -> float | None:
    try:
        n = float(num or 0)
        d = float(denom or 0)
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return None
    return round(n / d, 4)


async def seed_student_daily_features(
    db, weekly_rows: list[dict[str, Any]], dry_run: bool
) -> int:
    """One row per (user_id, date). Upsert-keyed."""
    if not weekly_rows:
        return 0
    rng = random.Random("daily-features")
    candidates = list(weekly_rows)
    rng.shuffle(candidates)
    # Cap to the most recent 4 weeks per user to keep volume sane.
    by_user_recent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(candidates, key=lambda r: r.get("week_start") or datetime.min):
        by_user_recent[str(row.get("user_id"))].append(row)
    docs: list[dict[str, Any]] = []
    for uid, rows in by_user_recent.items():
        for row in rows[-4:]:  # last 4 weekly snapshots per user
            docs.extend(_split_weekly_to_daily(row, rng))
    if not docs:
        return 0
    if dry_run:
        return len(docs)
    col = db["student_daily_features"]
    for doc in docs:
        await col.replace_one(
            {"user_id": doc["user_id"], "date": doc["date"]},
            doc,
            upsert=True,
        )
    return len(docs)


# --------------------------------------------------------------------------- #
# 2. course_engagement_features
# --------------------------------------------------------------------------- #


async def _drop_stale_indexes(db, col_name: str, expected_key_sets: set[tuple]) -> None:
    """Drop indexes that don't match the current schema.

    Older deployments may have a legacy index (e.g. ``course_id_1_date_1``
    on ``course_engagement_features``). When our seeded docs don't carry
    that field the index becomes a duplicate-key trap. We drop anything
    not in ``expected_key_sets`` so the upsert can proceed.
    """
    col = db[col_name]
    async for idx in col.list_indexes():
        name = idx.get("name")
        if name == "_id_":
            continue
        keys = tuple(sorted((k, int(v)) for k, v in (idx.get("key") or {}).items()))
        if keys not in expected_key_sets and name and not str(name).startswith("user_"):
            try:
                await col.drop_index(name)
                print(f"  dropped stale index {col_name}.{name} (keys={keys})")
            except Exception as exc:
                print(f"  warning: could not drop {col_name}.{name}: {exc}")


async def seed_course_engagement_features(
    db,
    weekly_rows: list[dict[str, Any]],
    course_lookup: dict[int, dict[str, Any]],
    dry_run: bool,
) -> int:
    """Aggregate weekly student rows into per-course-week rollups."""
    if not dry_run:
        # Sweep stale indexes from earlier schemas before our upsert
        # tries to write rows the legacy unique index can't accept.
        await _drop_stale_indexes(
            db,
            "course_engagement_features",
            expected_key_sets={
                (("course_id", 1), ("week_start", 1)),
                (("updated_at", -1),),
            },
        )
        await db["course_engagement_features"].create_index(
            [("course_id", 1), ("week_start", 1)],
            unique=True,
            name="course_id_1_week_start_1",
        )
    grouped: dict[tuple[int, datetime], list[dict[str, Any]]] = defaultdict(list)
    for row in weekly_rows:
        cid = row.get("course_id")
        ws = row.get("week_start")
        if not isinstance(cid, int) or not isinstance(ws, datetime):
            continue
        grouped[(cid, ws)].append(row)
    docs: list[dict[str, Any]] = []
    for (cid, ws), bucket in grouped.items():
        n = len(bucket)
        agg = {
            "active_students": n,
            "total_logins": int(
                sum(
                    float((r.get("features") or {}).get("logins") or 0) for r in bucket
                )
            ),
            "total_active_minutes": round(
                sum(
                    float((r.get("features") or {}).get("active_minutes") or 0.0)
                    for r in bucket
                ),
                2,
            ),
            "avg_attendance_rate": _avg(
                [
                    (r.get("features") or {}).get("attendance_rate")
                    for r in bucket
                ]
            ),
            "avg_submissions_per_student": round(
                _avg(
                    [
                        (r.get("features") or {}).get("submissions_total")
                        for r in bucket
                    ],
                    default=0.0,
                ),
                2,
            ),
            "late_submission_ratio": _ratio_sum(
                [
                    (r.get("features") or {}).get("submissions_late")
                    for r in bucket
                ],
                [
                    (r.get("features") or {}).get("submissions_total")
                    for r in bucket
                ],
            ),
            "avg_score_30d": _avg(
                [
                    (r.get("features") or {}).get("avg_score_30d")
                    for r in bucket
                ]
            ),
        }
        meta = course_lookup.get(cid) or {}
        docs.append(
            {
                "course_id": cid,
                "course_code": meta.get("course_code"),
                "course_title": meta.get("title"),
                "week_start": ws,
                "week_end": ws + timedelta(days=7),
                "features": agg,
                "source": SEED_SOURCE,
                "schema_version": 1,
                "updated_at": _utc_now(),
            }
        )
    if dry_run:
        return len(docs)
    col = db["course_engagement_features"]
    for doc in docs:
        await col.replace_one(
            {"course_id": doc["course_id"], "week_start": doc["week_start"]},
            doc,
            upsert=True,
        )
    return len(docs)


def _avg(values: list[Any], default: float | None = None) -> float | None:
    nums: list[float] = []
    for v in values:
        try:
            if v is None:
                continue
            nums.append(float(v))
        except (TypeError, ValueError):
            continue
    if not nums:
        return default
    return round(sum(nums) / len(nums), 4)


def _ratio_sum(num_list: list[Any], denom_list: list[Any]) -> float | None:
    n = sum(float(v or 0) for v in num_list)
    d = sum(float(v or 0) for v in denom_list)
    if d <= 0:
        return None
    return round(n / d, 4)


# --------------------------------------------------------------------------- #
# 3. competency_profiles
# --------------------------------------------------------------------------- #


_SUBJECT_FROM_CODE = re.compile(r"^([A-Z]{2,4})")


def _subject_code_from_course_code(code: str | None) -> str:
    """Use the course-code prefix as a coarse subject bucket.

    e.g. ``IT404 -> IT``, ``BA303 -> BA``. Falls back to ``GEN`` when
    the code doesn't fit the expected pattern.
    """
    if not code:
        return "GEN"
    m = _SUBJECT_FROM_CODE.match(code.strip().upper())
    return m.group(1) if m else "GEN"


async def seed_competency_profiles(
    db,
    weekly_rows: list[dict[str, Any]],
    course_lookup: dict[int, dict[str, Any]],
    dry_run: bool,
) -> int:
    """One profile per (user_id, subject_code).

    Subject = first 2-4 letters of the course code, so a student
    enrolled in IT404 + IT220 collapses into a single ``IT`` competency
    document. Strengths / weaknesses are derived from their average
    avg_score_30d and submissions-on-time ratio across that subject.
    """
    by_user_subject: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in weekly_rows:
        uid = str(row.get("user_id") or "")
        cid = row.get("course_id")
        meta = course_lookup.get(cid) if isinstance(cid, int) else None
        subject = _subject_code_from_course_code(
            (meta or {}).get("course_code")
        )
        if uid:
            by_user_subject[(uid, subject)].append(row)

    docs: list[dict[str, Any]] = []
    for (uid, subject), bucket in by_user_subject.items():
        scores = [
            float((r.get("features") or {}).get("avg_score_30d") or 0.0)
            for r in bucket
        ]
        scores = [s for s in scores if s > 0]
        on_time_ratio = _ratio_sum(
            [(r.get("features") or {}).get("submissions_on_time") for r in bucket],
            [(r.get("features") or {}).get("submissions_total") for r in bucket],
        )
        attendance = _avg(
            [(r.get("features") or {}).get("attendance_rate") for r in bucket]
        )
        avg_score = _avg(scores)

        # Bucket scores into mastery buckets used by the chat agent.
        if avg_score is None:
            mastery = "unknown"
        elif avg_score >= 75:
            mastery = "strong"
        elif avg_score >= 55:
            mastery = "developing"
        else:
            mastery = "weak"

        strengths = []
        weaknesses = []
        if mastery == "strong":
            strengths.append(f"{subject} performance is consistent")
        elif mastery == "weak":
            weaknesses.append(f"{subject} grades are trending below 55")
        if attendance is not None and attendance < 0.7:
            weaknesses.append("attendance dipped below 70%")
        elif attendance is not None and attendance >= 0.9:
            strengths.append("attendance is reliable")
        if on_time_ratio is not None and on_time_ratio < 0.6:
            weaknesses.append("more than 40% of submissions are late")
        elif on_time_ratio is not None and on_time_ratio >= 0.85:
            strengths.append("submissions are usually on time")

        docs.append(
            {
                "user_id": uid,
                "subject_code": subject,
                "mastery_level": mastery,
                "avg_score_30d": avg_score,
                "on_time_submission_ratio": on_time_ratio,
                "attendance_rate": attendance,
                "courses_observed": sorted(
                    {
                        (course_lookup.get(r.get("course_id")) or {}).get(
                            "course_code"
                        )
                        for r in bucket
                        if isinstance(r.get("course_id"), int)
                    }
                    - {None}
                ),
                "strengths": strengths,
                "weaknesses": weaknesses,
                "source": SEED_SOURCE,
                "schema_version": 1,
                "updated_at": _utc_now(),
            }
        )
    if dry_run:
        return len(docs)
    col = db["competency_profiles"]
    for doc in docs:
        await col.replace_one(
            {"user_id": doc["user_id"], "subject_code": doc["subject_code"]},
            doc,
            upsert=True,
        )
    return len(docs)


# --------------------------------------------------------------------------- #
# 4. learning_paths
# --------------------------------------------------------------------------- #


async def seed_learning_paths(
    db,
    weekly_rows: list[dict[str, Any]],
    course_lookup: dict[int, dict[str, Any]],
    dry_run: bool,
) -> int:
    """One ``status=active`` learning path doc per student.

    The plan is composed from the courses that user has weekly features
    for, ordered by most recent activity first. This isn't the live
    learning path (the API computes that on demand) — it's a frozen
    snapshot stored in the schema-correct shape.
    """
    by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in weekly_rows:
        uid = str(row.get("user_id") or "")
        cid = row.get("course_id")
        if uid and isinstance(cid, int):
            by_user[uid].append(row)

    docs: list[dict[str, Any]] = []
    now = _utc_now()
    for uid, rows in by_user.items():
        # Deduplicate to one row per course, keeping the most recent.
        latest_per_course: dict[int, dict[str, Any]] = {}
        for row in rows:
            cid = int(row.get("course_id"))
            existing = latest_per_course.get(cid)
            ws_existing = existing.get("week_start") if existing else None
            ws_new = row.get("week_start")
            if (
                existing is None
                or (
                    isinstance(ws_existing, datetime)
                    and isinstance(ws_new, datetime)
                    and ws_new > ws_existing
                )
            ):
                latest_per_course[cid] = row

        plan = []
        for idx, (cid, row) in enumerate(
            sorted(
                latest_per_course.items(),
                key=lambda kv: kv[1].get("week_start") or datetime.min,
                reverse=True,
            )[:6]
        ):
            meta = course_lookup.get(cid) or {}
            features = row.get("features") or {}
            avg = float(features.get("avg_score_30d") or 0.0)
            if avg >= 75:
                target_days = 7
                title = f"Continue {meta.get('course_code') or cid} at current pace"
            elif avg >= 55:
                target_days = 14
                title = f"Solidify fundamentals in {meta.get('course_code') or cid}"
            else:
                target_days = 21
                title = f"Review remedial material for {meta.get('course_code') or cid}"
            plan.append(
                {
                    "step_id": f"s{idx + 1}",
                    "title": title,
                    "course_id": cid,
                    "course_code": meta.get("course_code"),
                    "course_title": meta.get("title"),
                    "resource_refs": [
                        {"type": "course", "id": cid}
                    ],
                    "target_days": target_days,
                    "trigger": (
                        "remedial"
                        if avg < 55
                        else "accelerated" if avg >= 75 else "default"
                    ),
                }
            )

        major_hint = next(
            (
                _subject_code_from_course_code(
                    (course_lookup.get(int(r.get("course_id"))) or {}).get(
                        "course_code"
                    )
                )
                for r in rows
                if isinstance(r.get("course_id"), int)
            ),
            "GEN",
        )
        docs.append(
            {
                "user_id": uid,
                "path_version": 1,
                "status": "active",
                "generated_by": "demo_seed",
                "generated_at": now,
                "inputs": {
                    "subject_focus": major_hint,
                    "weekly_rows_used": len(rows),
                },
                "plan": plan,
                "explanation_ref": None,
                "source": SEED_SOURCE,
                "schema_version": 1,
            }
        )

    if dry_run:
        return len(docs)
    col = db["learning_paths"]
    # Archive any prior active demo path before inserting a new one so
    # we don't violate the "one active path per student" assumption the
    # design doc spells out.
    await col.update_many(
        {"source": SEED_SOURCE, "status": "active"},
        {"$set": {"status": "archived", "archived_at": now}},
    )
    if docs:
        await col.insert_many(docs)
    return len(docs)


# --------------------------------------------------------------------------- #
# 5. recommendation_explanations
# --------------------------------------------------------------------------- #


async def seed_recommendation_explanations(db, dry_run: bool) -> int:
    """One explanation row per existing risk_score.

    The bootstrap creates a unique index on ``risk_score_id`` for this
    collection — so we use the risk score's ``_id`` (or a hash of its
    natural key) as the linking field, and upsert on it to stay
    idempotent.
    """
    cursor = db["risk_scores"].find({}, {"_id": 1, "user_id": 1, "course_id": 1, "risk_level": 1, "top_factors": 1, "computed_at": 1})
    rows: list[dict[str, Any]] = []
    async for doc in cursor:
        rows.append(doc)

    docs: list[dict[str, Any]] = []
    now = _utc_now()
    for doc in rows:
        rid = doc.get("_id")
        natural_key = (
            f"{doc.get('user_id')}|{doc.get('course_id')}|"
            f"{(doc.get('computed_at') or '').isoformat() if isinstance(doc.get('computed_at'), datetime) else ''}"
        )
        risk_score_id = (
            str(rid)
            if rid is not None
            else hashlib.sha1(natural_key.encode()).hexdigest()
        )
        level = str(doc.get("risk_level") or "medium")
        factors = list(doc.get("top_factors") or [])

        # Pull the top driver (if any) into the prose body so each
        # explanation reads slightly differently.
        primary = factors[0] if factors else {}
        primary_name = str(primary.get("name") or "engagement")
        if level == "high":
            narrative = (
                f"This student is currently flagged at high risk. The dominant "
                f"signal is {primary_name}; recent attendance and submission "
                f"timeliness should be reviewed before the next assessment."
            )
        elif level == "medium":
            narrative = (
                f"Moderate risk: {primary_name} is trending in the wrong "
                f"direction, but engagement is still within normal bounds. A "
                f"light-touch nudge or office-hours invite is appropriate."
            )
        else:
            narrative = (
                f"Risk is currently low. {primary_name} looks healthy and the "
                f"student is on track — no intervention required this cycle."
            )

        docs.append(
            {
                "risk_score_id": risk_score_id,
                "user_id": doc.get("user_id"),
                "course_id": doc.get("course_id"),
                "risk_level": level,
                "narrative": narrative,
                "factors": factors[:3],
                "actions_suggested": _actions_for_level(level),
                "created_at": now,
                "source": SEED_SOURCE,
                "schema_version": 1,
            }
        )

    if dry_run:
        return len(docs)
    col = db["recommendation_explanations"]
    for doc in docs:
        await col.replace_one(
            {"risk_score_id": doc["risk_score_id"]},
            doc,
            upsert=True,
        )
    return len(docs)


def _actions_for_level(level: str) -> list[str]:
    if level == "high":
        return [
            "Schedule a 1:1 check-in with the student",
            "Send a remedial-resources notification",
            "Loop in their faculty advisor",
        ]
    if level == "medium":
        return [
            "Surface a learning-path nudge in the analytics view",
            "Recommend a study group session",
        ]
    return ["No action required — keep monitoring weekly trends"]


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


async def run(args: argparse.Namespace) -> int:
    db = get_mongo_ai_db()
    weekly_rows = await _load_weekly_features(db, max_users=args.max_users)
    if not weekly_rows:
        print(
            "No student_weekly_features rows found — run "
            "`python -m ml.data.seed_demo_cohort` first."
        )
        return 1
    print(
        f"Loaded {len(weekly_rows):,} weekly feature rows "
        f"({len({str(r.get('user_id')) for r in weekly_rows}):,} unique students)."
    )
    course_lookup = _course_lookup_from_supabase()
    print(f"Resolved {len(course_lookup):,} courses from Supabase.")

    targets = {
        "student_daily_features": seed_student_daily_features(
            db, weekly_rows, args.dry_run
        ),
        "course_engagement_features": seed_course_engagement_features(
            db, weekly_rows, course_lookup, args.dry_run
        ),
        "competency_profiles": seed_competency_profiles(
            db, weekly_rows, course_lookup, args.dry_run
        ),
        "learning_paths": seed_learning_paths(
            db, weekly_rows, course_lookup, args.dry_run
        ),
        "recommendation_explanations": seed_recommendation_explanations(
            db, args.dry_run
        ),
    }

    print()
    print(f"{'collection':32s}  rows {'(dry-run)' if args.dry_run else 'written'}")
    print("-" * 60)
    for name, coro in targets.items():
        n = await coro
        print(f"{name:32s}  {n:>6,d}")

    print()
    if args.dry_run:
        print("Dry-run complete. Re-run without --dry-run to persist.")
    else:
        print(
            "Done. To revert: db.<collection>.deleteMany({source: 'demo_seed'})."
        )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute counts without writing to Mongo.",
    )
    parser.add_argument(
        "--max-users",
        type=int,
        default=None,
        help="Cap the number of distinct users sourced from weekly features.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(_parse_args())))
