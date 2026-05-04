"""MongoDB data foundation helpers for Module 4 AI pipelines."""

from .contracts import (
    AssessmentEvent,
    AttendanceEvent,
    ActivityEvent,
    ContentEvent,
    FeatureInputSummary,
    RiskScoreDocument,
    StudentWeeklyFeatureSnapshot,
    SUPABASE_SOURCE_SPECS,
)
from .feature_jobs import WeeklyFeatureAggregator
from .ingestion import EventNormalizer, ExtractionWindow, Module4IngestionPlan, MongoEventWriter, SupabaseSourceReader



