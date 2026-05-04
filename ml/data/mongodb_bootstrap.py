"""Create the MongoDB collections and starter indexes for Module 4.

This script is intentionally idempotent. It does not insert application data.
It only prepares the collection layout used by the AI/analytics pipeline.

Run from the BE directory:
  python -m ml.data.mongodb_bootstrap

Or directly:
  python ml/data/mongodb_bootstrap.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.core.database import get_mongo_ai_db, get_mongo_raw_db


EVENT_COLLECTIONS = (
    "activity_events",
    "assessment_events",
    "content_events",
    "attendance_events",
    "chat_events",
    "ai_action_events",
)

FEATURE_COLLECTIONS = (
    "student_daily_features",
    "student_weekly_features",
    "course_engagement_features",
)

DECISION_COLLECTIONS = (
    "competency_profiles",
    "risk_scores",
    "learning_paths",
    "recommendation_explanations",
    "agent_runs",
)


async def ensure_collection(db, name: str) -> None:
    existing = await db.list_collection_names()
    if name not in existing:
        await db.create_collection(name)


async def bootstrap_activity_events(db) -> None:
    collection = db["activity_events"]
    await collection.create_index([("user_id", 1), ("event_time", -1)])
    await collection.create_index([("course_id", 1), ("event_time", -1)])
    await collection.create_index([("module_id", 1), ("event_time", -1)])
    await collection.create_index([("material_id", 1), ("event_time", -1)])
    await collection.create_index([("idempotency_key", 1)], unique=True)


async def bootstrap_assessment_events(db) -> None:
    collection = db["assessment_events"]
    await collection.create_index([("user_id", 1), ("event_time", -1)])
    await collection.create_index([("assignment_id", 1), ("event_time", -1)])
    await collection.create_index([("submission_id", 1), ("event_time", -1)])
    await collection.create_index([("timing_label", 1), ("event_time", -1)])
    await collection.create_index([("idempotency_key", 1)], unique=True)


async def bootstrap_content_events(db) -> None:
    collection = db["content_events"]
    await collection.create_index([("user_id", 1), ("event_time", -1)])
    await collection.create_index([("course_id", 1), ("event_time", -1)])
    await collection.create_index([("module_id", 1), ("event_time", -1)])
    await collection.create_index([("material_id", 1), ("event_time", -1)])
    await collection.create_index([("idempotency_key", 1)], unique=True)


async def bootstrap_attendance_events(db) -> None:
    collection = db["attendance_events"]
    await collection.create_index([("user_id", 1), ("event_time", -1)])
    await collection.create_index([("course_id", 1), ("event_time", -1)])
    await collection.create_index([("status", 1), ("event_time", -1)])
    await collection.create_index([("idempotency_key", 1)], unique=True)


async def bootstrap_chat_and_agent_events(db) -> None:
    chat = db["chat_events"]
    await chat.create_index([("conversation_id", 1), ("turn_id", 1)], unique=True)
    await chat.create_index([("user_id", 1), ("event_time", -1)])

    actions = db["ai_action_events"]
    await actions.create_index([("run_id", 1), ("event_time", -1)])
    await actions.create_index([("user_id", 1), ("event_time", -1)])
    await actions.create_index([("action_name", 1), ("event_time", -1)])
    await actions.create_index([("idempotency_key", 1)], unique=True)


async def bootstrap_student_daily_features(db) -> None:
    collection = db["student_daily_features"]
    await collection.create_index([("user_id", 1), ("date", 1)], unique=True)
    await collection.create_index([("updated_at", -1)])


async def bootstrap_student_weekly_features(db) -> None:
    collection = db["student_weekly_features"]
    await collection.create_index([("user_id", 1), ("week_start", 1)], unique=True)
    await collection.create_index([("course_id", 1), ("week_start", 1)])
    await collection.create_index([("updated_at", -1)])


async def bootstrap_course_engagement_features(db) -> None:
    collection = db["course_engagement_features"]
    await collection.create_index([("course_id", 1), ("week_start", 1)], unique=True)
    await collection.create_index([("updated_at", -1)])


async def bootstrap_competency_profiles(db) -> None:
    collection = db["competency_profiles"]
    await collection.create_index([("user_id", 1), ("subject_code", 1)], unique=True)
    await collection.create_index([("updated_at", -1)])


async def bootstrap_risk_scores(db) -> None:
    collection = db["risk_scores"]
    await collection.create_index([("user_id", 1), ("course_id", 1), ("computed_at", -1)])
    await collection.create_index([("risk_level", 1), ("computed_at", -1)])
    await collection.create_index([("model_version", 1), ("computed_at", -1)])


async def bootstrap_learning_paths(db) -> None:
    collection = db["learning_paths"]
    await collection.create_index([("user_id", 1), ("status", 1), ("generated_at", -1)])
    await collection.create_index([("path_version", 1), ("generated_at", -1)])


async def bootstrap_recommendation_explanations(db) -> None:
    collection = db["recommendation_explanations"]
    await collection.create_index([("user_id", 1), ("created_at", -1)])
    await collection.create_index([("risk_score_id", 1)], unique=True)


async def bootstrap_agent_runs(db) -> None:
    collection = db["agent_runs"]
    await collection.create_index([("run_id", 1)], unique=True)
    await collection.create_index([("user_id", 1), ("created_at", -1)])
    await collection.create_index([("status", 1), ("created_at", -1)])


async def main() -> int:
    raw_db = get_mongo_raw_db()
    ai_db = get_mongo_ai_db()

    for name in EVENT_COLLECTIONS:
        await ensure_collection(raw_db, name)
    for name in (*FEATURE_COLLECTIONS, *DECISION_COLLECTIONS):
        await ensure_collection(ai_db, name)

    await bootstrap_activity_events(raw_db)
    await bootstrap_assessment_events(raw_db)
    await bootstrap_content_events(raw_db)
    await bootstrap_attendance_events(raw_db)
    await bootstrap_chat_and_agent_events(raw_db)
    await bootstrap_student_daily_features(ai_db)
    await bootstrap_student_weekly_features(ai_db)
    await bootstrap_course_engagement_features(ai_db)
    await bootstrap_competency_profiles(ai_db)
    await bootstrap_risk_scores(ai_db)
    await bootstrap_learning_paths(ai_db)
    await bootstrap_recommendation_explanations(ai_db)
    await bootstrap_agent_runs(ai_db)

    print("MongoDB AI data foundation bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
