"""Typed contracts for the first MongoDB AI data layer.

These models define the shape of the raw events, rolling features, and risk
outputs that will be stored in MongoDB and consumed by the AI pipeline.

The module is intentionally small and dependency-light so it can be reused by
future ingestion jobs, validators, and tests.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class MongoBaseDocument(BaseModel):
    event_time: datetime
    source: Literal["web", "api", "job", "agent"]
    schema_version: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ActivityEvent(MongoBaseDocument):
    event_id: str
    event_type: Literal[
        "login",
        "logout",
        "page_view",
        "material_open",
        "material_close",
        "session_heartbeat",
    ]
    user_id: str
    course_id: int | None = None
    module_id: int | None = None
    material_id: int | None = None
    session_id: str | None = None
    duration_sec: int | None = Field(default=None, ge=0)
    properties: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str


class ContentEvent(MongoBaseDocument):
    event_id: str
    event_type: Literal["material_open", "material_close", "content_view", "content_download"]
    user_id: str
    course_id: int | None = None
    module_id: int | None = None
    material_id: int | None = None
    duration_sec: int | None = Field(default=None, ge=0)
    properties: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str


class AssessmentEvent(MongoBaseDocument):
    event_id: str
    event_type: Literal["submission_created", "submission_updated", "graded", "graded_finalized"]
    user_id: str
    course_id: int | None = None
    assignment_id: int | None = None
    submission_id: int | None = None
    timing_label: Literal["early", "on_time", "late", "blocked_by_hard_due"] | None = None
    final_score: float | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str


class AttendanceEvent(MongoBaseDocument):
    event_id: str
    event_type: Literal["attendance_marked", "attendance_updated", "session_attended", "session_absent"]
    user_id: str
    course_id: int | None = None
    status: str | None = None
    notes: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str


class StudentWeeklyFeatureSnapshot(BaseModel):
    user_id: str
    course_id: int | None = None
    week_start: datetime
    week_end: datetime
    source_event_max_time: datetime | None = None
    schema_version: int = 1
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    features: dict[str, float | int | bool | None] = Field(default_factory=dict)


class FeatureInputSummary(BaseModel):
    user_id: str
    course_id: int | None = None
    week_start: datetime
    week_end: datetime
    logins: int = 0
    active_minutes: float = 0.0
    materials_viewed: int = 0
    material_open_time_sec: float = 0.0
    submissions_total: int = 0
    submissions_on_time: int = 0
    submissions_late: int = 0
    attendance_rate: float | None = None
    absence_count: int = 0
    inactivity_streak_days: int = 0
    avg_score_30d: float | None = None
    score_trend_30d: float | None = None
    source_event_max_time: datetime | None = None


class RiskScoreDocument(BaseModel):
    user_id: str
    course_id: int | None = None
    computed_at: datetime
    model_version: str
    risk_score: float = Field(ge=0.0, le=1.0)
    risk_level: Literal["low", "medium", "high"]
    top_factors: list[dict[str, Any]] = Field(default_factory=list)
    feature_ref: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = 1


class SupabaseSourceSpec(BaseModel):
    table_name: str
    auth_mode: Literal["service_role", "user_token"]
    purpose: str
    target_collections: list[str]
    key_fields: list[str] = Field(default_factory=list)
    notes: str | None = None


RAW_EVENT_COLLECTION_BY_SOURCE = {
    "users": "activity_events",
    "courses": "activity_events",
    "modules": "content_events",
    "module_materials": "content_events",
    "assignments": "assessment_events",
    "assignment_submissions": "assessment_events",
    "assignment_submission_answers": "assessment_events",
    "course_attendance": "attendance_events",
    "course_enrollments": "activity_events",
    "notifications": "activity_events",
}


SUPABASE_SOURCE_SPECS: list[SupabaseSourceSpec] = [
    SupabaseSourceSpec(
        table_name="users",
        auth_mode="service_role",
        purpose="Identity and role lookup for all AI documents.",
        target_collections=["activity_events", "assessment_events", "attendance_events", "chat_events", "risk_scores"],
        key_fields=["user_id", "email", "role_id"],
    ),
    SupabaseSourceSpec(
        table_name="courses",
        auth_mode="service_role",
        purpose="Course context for course-linked events and predictions.",
        target_collections=["activity_events", "assessment_events", "attendance_events", "course_engagement_features", "risk_scores", "learning_paths"],
        key_fields=["id", "course_code", "lecturer_id"],
    ),
    SupabaseSourceSpec(
        table_name="modules",
        auth_mode="service_role",
        purpose="Module context for material interactions and assignment aggregation.",
        target_collections=["activity_events", "content_events", "assessment_events", "student_daily_features", "student_weekly_features"],
        key_fields=["id", "course_id"],
    ),
    SupabaseSourceSpec(
        table_name="module_materials",
        auth_mode="service_role",
        purpose="Material engagement inputs for behavior tracking.",
        target_collections=["activity_events", "content_events"],
        key_fields=["id", "module_id", "uploaded_by"],
    ),
    SupabaseSourceSpec(
        table_name="assignments",
        auth_mode="service_role",
        purpose="Assignment metadata for performance and timing labels.",
        target_collections=["assessment_events", "student_daily_features", "student_weekly_features", "risk_scores"],
        key_fields=["id", "module_id", "due_date", "hard_due_date"],
    ),
    SupabaseSourceSpec(
        table_name="assignment_submissions",
        auth_mode="service_role",
        purpose="Submission timing and grading labels.",
        target_collections=["assessment_events", "student_daily_features", "student_weekly_features", "risk_scores"],
        key_fields=["id", "student_id", "assignment_id", "status", "final_score"],
    ),
    SupabaseSourceSpec(
        table_name="assignment_submission_answers",
        auth_mode="service_role",
        purpose="Question-level performance and AI feedback inputs.",
        target_collections=["assessment_events", "competency_profiles"],
        key_fields=["id", "submission_id", "question_id", "is_correct", "earned_score"],
    ),
    SupabaseSourceSpec(
        table_name="course_attendance",
        auth_mode="service_role",
        purpose="Attendance trends for dropout risk and engagement features.",
        target_collections=["attendance_events", "student_daily_features", "student_weekly_features", "risk_scores"],
        key_fields=["id", "student_id", "course_id", "status", "session_date"],
    ),
    SupabaseSourceSpec(
        table_name="course_enrollments",
        auth_mode="service_role",
        purpose="Active course membership for scoping features and predictions.",
        target_collections=["student_daily_features", "student_weekly_features", "course_engagement_features", "learning_paths"],
        key_fields=["course_id", "student_id"],
    ),
    SupabaseSourceSpec(
        table_name="notifications",
        auth_mode="service_role",
        purpose="Message and reminder context for activity and chatbot signals.",
        target_collections=["activity_events", "chat_events"],
        key_fields=["id", "recipient_id", "course_id", "scenario"],
    ),
]
