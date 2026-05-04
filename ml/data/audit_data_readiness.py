#!/usr/bin/env python3
"""Audit Mongo data readiness for AI pipeline use.

Usage (from BE/, venv on):
  python -m ml.data.audit_data_readiness
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.core.database import get_mongo_ai_db, get_mongo_raw_db


@dataclass(slots=True)
class CollectionCheck:
    name: str
    layer: str
    required: bool
    min_rows: int
    purpose: str


RAW_CHECKS = [
    CollectionCheck("activity_events", "raw", True, 100, "behavior timeline signals"),
    CollectionCheck("assessment_events", "raw", True, 50, "scores + submission timing"),
    CollectionCheck("attendance_events", "raw", True, 20, "attendance trend"),
    CollectionCheck("content_events", "raw", False, 20, "material interaction detail"),
]

AI_CHECKS = [
    CollectionCheck("student_daily_features", "ai", False, 20, "optional short-window features"),
    CollectionCheck("student_weekly_features", "ai", True, 100, "main training/inference dataset"),
    CollectionCheck("course_engagement_features", "ai", False, 20, "course-level analytics"),
    CollectionCheck("risk_scores", "ai", False, 1, "stored inference outputs"),
    CollectionCheck("competency_profiles", "ai", False, 1, "stored competency analysis"),
    CollectionCheck("learning_paths", "ai", False, 1, "stored recommendations"),
]


async def _count(db, collection_name: str) -> int:
    return int(await db[collection_name].count_documents({}))


async def _run_checks(db, checks: list[CollectionCheck]) -> tuple[list[dict], int]:
    out: list[dict] = []
    required_failures = 0
    existing = set(await db.list_collection_names())
    for c in checks:
        exists = c.name in existing
        count = await _count(db, c.name) if exists else 0
        status = "ready" if count >= c.min_rows else "low_or_empty"
        if c.required and status != "ready":
            required_failures += 1
        out.append(
            {
                "collection": c.name,
                "required": c.required,
                "rows": count,
                "min_rows": c.min_rows,
                "status": status,
                "purpose": c.purpose,
            }
        )
    return out, required_failures


async def main() -> int:
    raw_db = get_mongo_raw_db()
    ai_db = get_mongo_ai_db()

    raw_rows, raw_fail = await _run_checks(raw_db, RAW_CHECKS)
    ai_rows, ai_fail = await _run_checks(ai_db, AI_CHECKS)

    print("=== AI Data Readiness Audit ===")
    print(f"raw_db: {raw_db.name}")
    print(f"ai_db: {ai_db.name}")
    print()

    print("[RAW LAYER]")
    for r in raw_rows:
        req = "required" if r["required"] else "optional"
        print(
            f"- {r['collection']}: {r['status']} ({req}) rows={r['rows']} min={r['min_rows']} | {r['purpose']}"
        )
    print()

    print("[AI LAYER]")
    for r in ai_rows:
        req = "required" if r["required"] else "optional"
        print(
            f"- {r['collection']}: {r['status']} ({req}) rows={r['rows']} min={r['min_rows']} | {r['purpose']}"
        )
    print()

    total_fail = raw_fail + ai_fail
    if total_fail == 0:
        print("overall_status: READY")
        print("next_step: run model pipeline on REAL_MONGO data.")
        return 0

    print("overall_status: NOT_READY")
    print("next_step: backfill weekly features and/or ingest raw events before trusting model quality.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
