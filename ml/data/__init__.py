"""Data layer helpers for the ML + seeding pipeline.

Only the artefacts that other production code or the seeding scripts
import are re-exported here. The per-script entry points (seeders,
crawler, etc.) live as standalone modules — run them with
``python -m ml.data.<module>``.
"""

from .contracts import (
    ActivityEvent,
    AssessmentEvent,
    AttendanceEvent,
    ContentEvent,
    FeatureInputSummary,
    RiskScoreDocument,
    StudentWeeklyFeatureSnapshot,
    SUPABASE_SOURCE_SPECS,
)
from .feature_jobs import WeeklyFeatureAggregator

__all__ = [
    "ActivityEvent",
    "AssessmentEvent",
    "AttendanceEvent",
    "ContentEvent",
    "FeatureInputSummary",
    "RiskScoreDocument",
    "StudentWeeklyFeatureSnapshot",
    "SUPABASE_SOURCE_SPECS",
    "WeeklyFeatureAggregator",
]
