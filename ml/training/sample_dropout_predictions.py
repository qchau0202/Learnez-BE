#!/usr/bin/env python3
"""Score current students for dropout risk, print a sample, and persist results.

Reads the latest ``student_weekly_features`` row per ``(user_id, course_id)`` in
the lookback window, runs the trained classifier, and (by default) upserts the
output into ``learnez_ai.risk_scores`` so the analytics API can serve cached
predictions instead of running the model live.

Usage (from BE/, venv on):

  # Score and persist (default), print a 10-student sample
  python -m ml.training.sample_dropout_predictions

  # Inspect only — do not write to Mongo
  python -m ml.training.sample_dropout_predictions --no-persist

  # Lookback window and sample size are tunable
  python -m ml.training.sample_dropout_predictions --since-weeks 12 --sample-size 20
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
from pymongo.errors import AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError

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


def _summary_for(features: dict[str, Any], risk_level: str) -> str:
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


async def _load_latest_rows(since_weeks: int, *, batch_size: int = 500) -> list[dict[str, Any]]:
    """Stream ``student_weekly_features`` and keep the latest doc per (user, course).

    We deliberately avoid ``cursor.to_list(length=N)`` for large collections
    because it forces the server to materialise N docs before responding, which
    on Atlas trips the 30-second ``socketTimeoutMS``. Streaming with
    ``async for`` paces requests so each batch round-trip is well under that.
    """
    db = get_mongo_ai_db()
    end = datetime.now(timezone.utc)
    start = end - timedelta(weeks=since_weeks)
    projection = {
        "user_id": 1,
        "course_id": 1,
        "week_start": 1,
        "week_end": 1,
        "source_event_max_time": 1,
        "features": 1,
    }
    latest_by_key: dict[tuple[str, Any], dict[str, Any]] = {}

    attempts = 0
    while True:
        try:
            cursor = (
                db["student_weekly_features"]
                .find({"week_start": {"$gte": start, "$lt": end}}, projection=projection)
                .batch_size(batch_size)
            )
            scanned = 0
            async for doc in cursor:
                scanned += 1
                uid = str(doc.get("user_id") or "").strip()
                if not uid:
                    continue
                key = (uid, doc.get("course_id"))
                prev = latest_by_key.get(key)
                if prev is None:
                    latest_by_key[key] = doc
                    continue
                # Keep whichever doc has the more recent week_start.
                prev_ws = prev.get("week_start")
                this_ws = doc.get("week_start")
                if isinstance(this_ws, datetime) and (
                    not isinstance(prev_ws, datetime) or this_ws > prev_ws
                ):
                    latest_by_key[key] = doc
                if scanned % 5000 == 0:
                    print(f"[score] scanned={scanned} unique_(user,course)={len(latest_by_key)}")
            print(f"[score] scan complete: scanned={scanned} unique_(user,course)={len(latest_by_key)}")
            return list(latest_by_key.values())
        except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError) as exc:
            attempts += 1
            if attempts >= 4:
                raise
            wait = 1.0 * attempts
            print(f"[score] transient mongo error ({type(exc).__name__}); retry {attempts}/3 after {wait:.1f}s")
            await asyncio.sleep(wait)


async def _ensure_risk_scores_indexes() -> None:
    db = get_mongo_ai_db()
    col = db["risk_scores"]
    # Compound key keeps "current" row per (user, course); we upsert on this.
    await col.create_index(
        [("user_id", 1), ("course_id", 1)],
        unique=True,
        name="user_id_1_course_id_1",
    )
    # Useful for time-windowed queries from the API.
    await col.create_index([("predicted_at", -1)], name="predicted_at_desc")


async def _persist_risk_scores(
    docs: list[dict[str, Any]],
    *,
    model_version: str,
    chunk_size: int = 200,
) -> int:
    if not docs:
        return 0
    db = get_mongo_ai_db()
    col = db["risk_scores"]
    written = 0
    for start_idx in range(0, len(docs), chunk_size):
        chunk = docs[start_idx : start_idx + chunk_size]
        for d in chunk:
            attempts = 0
            while True:
                try:
                    await col.replace_one(
                        {"user_id": d["user_id"], "course_id": d.get("course_id")},
                        d,
                        upsert=True,
                    )
                    written += 1
                    break
                except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError) as exc:
                    attempts += 1
                    if attempts >= 4:
                        raise
                    wait = 1.0 * attempts
                    print(
                        f"[score] persist retry {attempts}/3 after {wait:.1f}s "
                        f"({type(exc).__name__})"
                    )
                    await asyncio.sleep(wait)
        if (start_idx // chunk_size) % 5 == 0:
            print(f"[score] persisted {min(start_idx + chunk_size, len(docs))}/{len(docs)}")
    return written


def _score_one(
    model: Any,
    features: dict[str, Any],
    low_max: float,
    med_max: float,
) -> tuple[float, str, float, dict[int, float]]:
    vector = [[float(features.get(col) or 0.0) for col in FEATURE_COLUMNS]]
    if hasattr(model, "predict_proba"):
        probs = model.predict_proba(vector)[0]
        classes = [int(x) for x in model.classes_.tolist()] if hasattr(model, "classes_") else []
        proba_by_class = {classes[i]: float(probs[i]) for i in range(len(classes))} if len(classes) == len(probs) else {}
    else:
        proba_by_class = {}
    score = score_from_probabilities(proba_by_class)
    label = risk_level_from_score(score, low_max, med_max)
    confidence = max(proba_by_class.values(), default=0.5)
    return score, label, confidence, proba_by_class


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Score random users + persist risk_scores so the analytics API can serve cached predictions."
    )
    p.add_argument("--sample-size", type=int, default=10, help="How many users to print in the sample.")
    p.add_argument("--since-weeks", type=int, default=12, help="Lookback window for latest user snapshots.")
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
    p.add_argument(
        "--no-persist",
        dest="persist",
        action="store_false",
        help="Skip writing risk_scores; only print the sample.",
    )
    p.set_defaults(persist=True)
    return p.parse_args()


async def _amain(args: argparse.Namespace) -> int:
    if not args.model_path.exists():
        print(f"Model not found: {args.model_path}")
        return 1

    rows = await _load_latest_rows(args.since_weeks)
    if not rows:
        print("No student_weekly_features rows found in selected time window.")
        return 1

    model = joblib.load(args.model_path)
    low_max, med_max = load_thresholds(args.thresholds_path)
    model_version = args.model_path.stem
    now_utc = datetime.now(timezone.utc)

    risk_counts = {"LOW": 0, "MEDIUM": 0, "HIGH": 0}
    confidence_values: list[float] = []
    risk_docs: list[dict[str, Any]] = []
    scored_rows: list[dict[str, Any]] = []

    for doc in rows:
        uid = str(doc.get("user_id"))
        course_id = doc.get("course_id")
        week_start = doc.get("week_start")
        features = dict(doc.get("features") or {})
        score, label, confidence, _proba = _score_one(model, features, low_max, med_max)
        risk_counts[label.upper()] += 1
        confidence_values.append(confidence)
        scored_rows.append(
            {
                "user_id": uid,
                "course_id": course_id,
                "week_start": week_start,
                "features": features,
                "risk_score": score,
                "risk_level": label,
                "confidence": confidence,
            }
        )
        if args.persist:
            metrics = {
                "attendance_rate": float(features.get("attendance_rate") or 0.0),
                "avg_score_30d": float(features.get("avg_score_30d") or 0.0),
                "inactivity_streak_days": int(features.get("inactivity_streak_days") or 0),
                "submissions_total": int(features.get("submissions_total") or 0),
                "submissions_late": int(features.get("submissions_late") or 0),
                "active_minutes": float(features.get("active_minutes") or 0.0),
                "logins": int(features.get("logins") or 0),
            }
            risk_docs.append(
                {
                    "user_id": uid,
                    "course_id": course_id,
                    "risk_score": round(float(score), 4),
                    "risk_level": label,
                    "model_version": model_version,
                    "predicted_at": now_utc,
                    "created_at": now_utc,
                    "week_start": week_start,
                    "features": features,
                    "metrics": metrics,
                    "summary": _summary_for(features, label),
                    "schema_version": 1,
                }
            )

    persisted = 0
    if args.persist and risk_docs:
        await _ensure_risk_scores_indexes()
        persisted = await _persist_risk_scores(risk_docs, model_version=model_version)

    rng = random.Random(args.seed)
    sample_size = min(args.sample_size, len(scored_rows))
    sample = rng.sample(scored_rows, sample_size)

    print()
    print("=== Random Student Risk Sample ===")
    print(f"model_path: {args.model_path}")
    print(f"thresholds_path: {args.thresholds_path} | low_max={low_max:.3f} medium_max={med_max:.3f}")
    print(f"candidate_rows: {len(scored_rows)} | sampled_users: {sample_size}")
    if args.persist:
        print(f"persisted_to_risk_scores: {persisted}")
    else:
        print("persisted_to_risk_scores: (skipped — --no-persist)")
    print()

    for row in sample:
        risks, strengths = _student_friendly_reasons(row["features"])
        print(
            f"- user_id={row['user_id']} | course_id={row.get('course_id')} | "
            f"week_start={row['week_start']} | risk={row['risk_level'].upper()} | "
            f"confidence={row['confidence']:.3f}"
        )
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
        f"risk_distribution (all rows): LOW={risk_counts['LOW']} "
        f"MEDIUM={risk_counts['MEDIUM']} HIGH={risk_counts['HIGH']}"
    )
    print(
        f"avg_model_confidence: {mean(confidence_values):.3f}"
        if confidence_values
        else "avg_model_confidence: n/a"
    )
    print("note: use this with lecturer review and weekly trend, not as a standalone decision.")
    return 0


def main() -> int:
    return asyncio.run(_amain(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
