#!/usr/bin/env python3
"""Calibrate dropout risk thresholds from real prediction score distribution.

Usage (from BE/, venv on):
  python -m ml.training.calibrate_dropout_thresholds --since-weeks 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np

from app.core.database import get_mongo_ai_db
from ml.training.dataset_builder import FEATURE_COLUMNS
from ml.training.risk_bands import risk_level_from_score, score_from_probabilities


def _threshold_from_rank(sorted_scores: list[float], fraction: float) -> float:
    if not sorted_scores:
        return 0.5
    idx = int(round((len(sorted_scores) - 1) * fraction))
    idx = max(0, min(len(sorted_scores) - 1, idx))
    return float(sorted_scores[idx])


def _next_distinct(sorted_scores: list[float], value: float) -> float | None:
    for s in sorted_scores:
        if s > value:
            return float(s)
    return None


async def _load_latest_rows(since_weeks: int, cap_docs: int = 120000) -> list[dict[str, Any]]:
    db = get_mongo_ai_db()
    end = datetime.now(timezone.utc)
    start = end - timedelta(weeks=since_weeks)
    docs = await (
        db["student_weekly_features"]
        .find({"week_start": {"$gte": start, "$lt": end}})
        .sort([("week_start", -1)])
        .to_list(length=cap_docs)
    )
    latest_by_user: dict[str, dict[str, Any]] = {}
    for d in docs:
        uid = str(d.get("user_id") or "").strip()
        if uid and uid not in latest_by_user:
            latest_by_user[uid] = d
    return list(latest_by_user.values())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Calibrate low/medium/high thresholds from real data.")
    p.add_argument("--since-weeks", type=int, default=20)
    p.add_argument("--q-low", type=float, default=0.35, help="Quantile for low/medium boundary.")
    p.add_argument("--q-medium", type=float, default=0.75, help="Quantile for medium/high boundary.")
    p.add_argument("--model-path", type=Path, default=Path("ml/models/dropout_rf_composite.joblib"))
    p.add_argument("--out", type=Path, default=Path("ml/models/dropout_thresholds_composite.json"))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.model_path.exists():
        print(f"Model not found: {args.model_path}")
        return 1
    if not (0.0 < args.q_low < args.q_medium < 1.0):
        print("Invalid quantiles: require 0 < q-low < q-medium < 1")
        return 1

    rows = asyncio.run(_load_latest_rows(args.since_weeks))
    if not rows:
        print("No student rows found in student_weekly_features.")
        return 1

    model = joblib.load(args.model_path)
    scores: list[float] = []
    for d in rows:
        feats = dict(d.get("features") or {})
        vector = [[float(feats.get(c) or 0.0) for c in FEATURE_COLUMNS]]
        probs = model.predict_proba(vector)[0] if hasattr(model, "predict_proba") else None
        classes = [int(x) for x in model.classes_.tolist()] if hasattr(model, "classes_") else []
        proba_by_class = {classes[i]: float(probs[i]) for i in range(len(classes))} if probs is not None else {}
        scores.append(score_from_probabilities(proba_by_class))

    sorted_scores = sorted(float(s) for s in scores)
    low_max = _threshold_from_rank(sorted_scores, args.q_low)
    medium_max = _threshold_from_rank(sorted_scores, args.q_medium)

    # If score values are concentrated, force medium threshold to next distinct value.
    if medium_max <= low_max:
        nxt = _next_distinct(sorted_scores, low_max)
        if nxt is not None:
            medium_max = nxt
        else:
            # Fully collapsed score distribution: fallback to conservative defaults.
            low_max, medium_max = 0.40, 0.70

    dist = {"low": 0, "medium": 0, "high": 0}
    for s in scores:
        dist[risk_level_from_score(s, low_max, medium_max)] += 1

    payload = {
        "calibrated_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_path": str(args.model_path),
        "rows_used": len(scores),
        "q_low": args.q_low,
        "q_medium": args.q_medium,
        "low_max": round(low_max, 6),
        "medium_max": round(medium_max, 6),
        "distribution_after_calibration": dist,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")

    print(f"Saved thresholds: {args.out}")
    print(f"rows_used: {len(scores)}")
    print(f"low_max={low_max:.4f} medium_max={medium_max:.4f}")
    print(
        "distribution_after_calibration:",
        f"low={dist['low']} medium={dist['medium']} high={dist['high']}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
