#!/usr/bin/env python3
"""Sample random students and explain dropout risk in plain language.

Usage (from BE/, venv on):
  python -m ml.training.sample_dropout_predictions --sample-size 10
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import joblib

from app.core.database import get_mongo_ai_db
from ml.training.dataset_builder import FEATURE_COLUMNS
from ml.training.risk_bands import load_thresholds, risk_level_from_score, score_from_probabilities


def _student_friendly_reasons(features: dict[str, Any]) -> tuple[list[str], list[str]]:
    risks: list[str] = []
    strengths: list[str] = []

    attendance = float(features.get("attendance_rate") or 0.0)
    inactivity_days = int(features.get("inactivity_streak_days") or 0)
    subs_total = float(features.get("submissions_total") or 0.0)
    subs_late = float(features.get("submissions_late") or 0.0)
    avg_score = float(features.get("avg_score_30d") or 0.0)
    active_minutes = float(features.get("active_minutes") or 0.0)
    logins = float(features.get("logins") or 0.0)

    late_ratio = (subs_late / subs_total) if subs_total > 0 else 0.0

    if attendance < 0.6:
        risks.append("attendance is low recently")
    if inactivity_days >= 7:
        risks.append("long inactivity streak was detected")
    if subs_total >= 3 and late_ratio >= 0.5:
        risks.append("many submissions are late")
    if avg_score < 55:
        risks.append("recent average score is low")
    if active_minutes < 45 or logins < 3:
        risks.append("platform engagement is low")

    if attendance >= 0.8:
        strengths.append("attendance is consistently good")
    if subs_total >= 3 and late_ratio <= 0.2:
        strengths.append("submissions are mostly on time")
    if avg_score >= 75:
        strengths.append("recent academic performance is strong")
    if active_minutes >= 90 and logins >= 6:
        strengths.append("learning engagement is healthy")

    return risks, strengths


async def _load_latest_rows(since_weeks: int, cap_docs: int = 60000) -> list[dict[str, Any]]:
    db = get_mongo_ai_db()
    end = datetime.now(timezone.utc)
    start = end - timedelta(weeks=since_weeks)

    cursor = (
        db["student_weekly_features"]
        .find({"week_start": {"$gte": start, "$lt": end}})
        .sort([("week_start", -1)])
    )
    docs = await cursor.to_list(length=cap_docs)

    latest_by_user: dict[str, dict[str, Any]] = {}
    for d in docs:
        uid = str(d.get("user_id") or "").strip()
        if not uid or uid in latest_by_user:
            continue
        latest_by_user[uid] = d
    return list(latest_by_user.values())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sample random users and print risk predictions with explanations.")
    p.add_argument("--sample-size", type=int, default=10, help="How many random users to show.")
    p.add_argument("--since-weeks", type=int, default=20, help="Lookback window for latest user snapshots.")
    p.add_argument(
        "--model-path",
        type=Path,
        default=Path("ml/models/dropout_rf_composite.joblib"),
        help="Path to a trained joblib model.",
    )
    p.add_argument(
        "--thresholds-path",
        type=Path,
        default=Path("ml/models/dropout_thresholds_composite.json"),
        help="Calibrated thresholds JSON. Defaults are used if missing.",
    )
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.model_path.exists():
        print(f"Model not found: {args.model_path}")
        return 1

    rows = asyncio.run(_load_latest_rows(args.since_weeks))
    if not rows:
        print("No student_weekly_features rows found in selected time window.")
        return 1

    rng = random.Random(args.seed)
    sample_size = min(args.sample_size, len(rows))
    sample = rng.sample(rows, sample_size)

    model = joblib.load(args.model_path)
    low_max, med_max = load_thresholds(args.thresholds_path)

    risk_counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0}
    confidence_values: list[float] = []

    print("=== Random Student Risk Sample ===")
    print(f"model_path: {args.model_path}")
    print(f"thresholds_path: {args.thresholds_path} | low_max={low_max:.3f} medium_max={med_max:.3f}")
    print(f"candidate_users: {len(rows)} | sampled_users: {sample_size}")
    print()

    for doc in sample:
        uid = str(doc.get("user_id"))
        week_start = doc.get("week_start")
        features = dict(doc.get("features") or {})
        vector = [[float(features.get(col) or 0.0) for col in FEATURE_COLUMNS]]

        probs = model.predict_proba(vector)[0] if hasattr(model, "predict_proba") else []
        classes = [int(x) for x in model.classes_.tolist()] if hasattr(model, "classes_") else []
        proba_by_class = {classes[i]: float(probs[i]) for i in range(len(classes))} if len(classes) == len(probs) else {}
        score = score_from_probabilities(proba_by_class)
        label = risk_level_from_score(score, low_max, med_max).upper()
        confidence = max(proba_by_class.values(), default=0.5)

        risks, strengths = _student_friendly_reasons(features)
        risk_counts[label] = risk_counts.get(label, 0) + 1
        confidence_values.append(confidence)

        print(f"- user_id={uid} | week_start={week_start} | risk={label} | confidence={confidence:.3f}")
        if risks:
            print(f"  why_risk: {', '.join(risks)}")
        if strengths:
            print(f"  positive_signals: {', '.join(strengths)}")
        if not risks and not strengths:
            print("  why: limited signals this week; monitor next weeks for trend.")
        print("  message_for_student: This is an early warning signal, not a final judgment.")
        print()

    print("=== Sample Summary ===")
    print(
        f"risk_distribution: LOW={risk_counts['LOW']} MEDIUM={risk_counts['MEDIUM']} HIGH={risk_counts['HIGH']}"
    )
    print(f"avg_model_confidence: {mean(confidence_values):.3f}" if confidence_values else "avg_model_confidence: n/a")
    print("note: use this with lecturer review and weekly trend, not as a standalone decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
