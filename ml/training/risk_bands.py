"""Shared helpers for calibrated risk score banding."""

from __future__ import annotations

import json
from pathlib import Path


def score_from_probabilities(proba_by_class: dict[int, float]) -> float:
    """Convert class probabilities into a 0..1 scalar risk score."""
    if not proba_by_class:
        return 0.5
    classes = sorted(proba_by_class.keys())
    if len(classes) <= 1:
        return 0.5
    c_min = classes[0]
    c_max = classes[-1]
    denom = float(max(c_max - c_min, 1))
    weighted = 0.0
    for cls, prob in proba_by_class.items():
        normalized = (float(cls) - float(c_min)) / denom
        weighted += normalized * float(prob)
    return max(0.0, min(1.0, weighted))


def default_thresholds() -> tuple[float, float]:
    return 0.40, 0.70


def load_thresholds(path: Path) -> tuple[float, float]:
    if not path.exists():
        return default_thresholds()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        low_max = float(payload.get("low_max", 0.40))
        med_max = float(payload.get("medium_max", 0.70))
        low_max = max(0.0, min(1.0, low_max))
        med_max = max(low_max, min(1.0, med_max))
        return low_max, med_max
    except (ValueError, TypeError, json.JSONDecodeError):
        return default_thresholds()


def risk_level_from_score(score: float, low_max: float, med_max: float) -> str:
    if score <= low_max:
        return "low"
    if score <= med_max:
        return "medium"
    return "high"
