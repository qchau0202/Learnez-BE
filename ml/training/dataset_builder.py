"""Build model-ready training data from the MongoDB feature layer.

Flow: raw events → ``student_weekly_features`` → flat rows for sklearn.

Label modes (see ``build_training_dataframe``):

- **composite** — multi-factor ternary risk (academic + engagement + attendance), with **academic
  rescue** so strong assignment/score signals are not drowned by low app usage alone.
- **persona_multiclass** — six simulation archetypes (orthogonal persona id; good for honest metrics).
- **persona_binary** — high/low persona groups.
- **proxy** — legacy single-threshold heuristic (inflated accuracy if features overlap).

See ``FEATURE_GROUPS`` for how columns map to engagement vs academic vs attendance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Literal
import random

from app.core.database import get_mongo_ai_db, get_mongo_raw_db


LabelMode = Literal["composite", "persona_multiclass", "persona_binary", "persona", "proxy"]

# Personas from simulation seed — stable order for multi-class targets (indices 0..5).
PERSONA_CLASS_ORDER: tuple[str, ...] = ("star", "steady", "uneven", "struggling", "sparse", "dormant")
PERSONA_TO_INDEX: dict[str, int] = {p: i for i, p in enumerate(PERSONA_CLASS_ORDER)}

# Binary persona groups (for persona_binary / legacy "persona" CLI).
HIGH_RISK_PERSONAS = frozenset({"struggling", "sparse", "dormant", "uneven"})
LOW_RISK_PERSONAS = frozenset({"star", "steady"})

# Human-readable grouping of inputs (model still uses flat FEATURE_COLUMNS).
FEATURE_GROUPS: dict[str, list[str]] = {
    "engagement": ["logins", "active_minutes", "materials_viewed", "material_open_time_sec"],
    "academic_work": ["submissions_total", "submissions_on_time", "submissions_late", "avg_score_30d", "score_trend_30d"],
    "attendance": ["attendance_rate", "absence_count", "inactivity_streak_days"],
}

FEATURE_COLUMNS = [
    "logins",
    "active_minutes",
    "materials_viewed",
    "material_open_time_sec",
    "submissions_total",
    "submissions_on_time",
    "submissions_late",
    "attendance_rate",
    "absence_count",
    "inactivity_streak_days",
    "avg_score_30d",
    "score_trend_30d",
]


@dataclass(slots=True)
class TrainingFrame:
    rows: list[dict[str, Any]]
    label_column: str
    feature_columns: list[str]

    def __len__(self) -> int:
        return len(self.rows)


@dataclass(slots=True)
class DataCleaningSummary:
    input_rows: int
    output_rows: int
    duplicate_rows_removed: int
    rows_missing_identity: int
    rows_missing_label: int


@dataclass(slots=True)
class DataQualitySummary:
    row_count: int
    user_count: int
    course_count: int
    label_counts: dict[str, int]
    missing_by_feature: dict[str, int]
    feature_stats: dict[str, dict[str, float]]


def _coerce_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def clean_training_rows(
    rows: list[dict[str, Any]],
    feature_columns: list[str],
    label_column: str,
) -> tuple[list[dict[str, Any]], DataCleaningSummary]:
    cleaned_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicate_rows_removed = 0
    rows_missing_identity = 0
    rows_missing_label = 0

    for row in rows:
        user_id = str(row.get("user_id") or "").strip()
        week_start = row.get("week_start")
        course_id = str(row.get("course_id") or "").strip()
        if not user_id or week_start is None:
            rows_missing_identity += 1
            continue

        label = row.get(label_column)
        if label is None:
            rows_missing_label += 1
            continue

        normalized = dict(row)
        normalized["user_id"] = user_id
        normalized["course_id"] = row.get("course_id")
        normalized["week_start"] = week_start
        normalized["week_end"] = row.get("week_end")
        normalized["source_event_max_time"] = row.get("source_event_max_time")
        normalized[label_column] = int(label)
        for column in feature_columns:
            normalized[column] = _coerce_float(normalized.get(column))

        key = (user_id, str(week_start), course_id)
        existing = cleaned_by_key.get(key)
        if existing is None:
            cleaned_by_key[key] = normalized
            continue

        duplicate_rows_removed += 1
        existing_time = existing.get("source_event_max_time")
        new_time = normalized.get("source_event_max_time")
        if existing_time is None or (new_time is not None and str(new_time) > str(existing_time)):
            cleaned_by_key[key] = normalized

    cleaned = sorted(cleaned_by_key.values(), key=lambda item: (str(item.get("week_start")), str(item.get("user_id"))))
    summary = DataCleaningSummary(
        input_rows=len(rows),
        output_rows=len(cleaned),
        duplicate_rows_removed=duplicate_rows_removed,
        rows_missing_identity=rows_missing_identity,
        rows_missing_label=rows_missing_label,
    )
    return cleaned, summary


def summarize_training_rows(
    rows: list[dict[str, Any]],
    feature_columns: list[str],
    label_column: str,
) -> DataQualitySummary:
    label_counts: dict[str, int] = {}
    missing_by_feature: dict[str, int] = {column: 0 for column in feature_columns}
    feature_values: dict[str, list[float]] = {column: [] for column in feature_columns}
    users: set[str] = set()
    courses: set[str] = set()

    for row in rows:
        user_id = str(row.get("user_id") or "").strip()
        if user_id:
            users.add(user_id)
        course_id = row.get("course_id")
        if course_id is not None:
            courses.add(str(course_id))

        label = str(int(row.get(label_column) or 0))
        label_counts[label] = label_counts.get(label, 0) + 1

        for column in feature_columns:
            value = row.get(column)
            if value is None:
                missing_by_feature[column] += 1
                continue
            feature_values[column].append(_coerce_float(value))

    feature_stats: dict[str, dict[str, float]] = {}
    for column, values in feature_values.items():
        if not values:
            feature_stats[column] = {"min": 0.0, "max": 0.0, "mean": 0.0}
            continue
        feature_stats[column] = {
            "min": float(min(values)),
            "max": float(max(values)),
            "mean": float(mean(values)),
        }

    return DataQualitySummary(
        row_count=len(rows),
        user_count=len(users),
        course_count=len(courses),
        label_counts=label_counts,
        missing_by_feature=missing_by_feature,
        feature_stats=feature_stats,
    )


class TrainingDatasetBuilder:
    """Convert `student_weekly_features` into a supervised training frame."""

    def __init__(self, mongo_db=None) -> None:
        self._db = mongo_db or get_mongo_ai_db()

    async def load_student_personas(self) -> dict[str, str]:
        """Map user_id → persona for simulated students (raw `simulation_users`, role_id=3)."""
        raw = get_mongo_raw_db()
        cursor = raw["simulation_users"].find({"role_id": 3}, {"user_id": 1, "persona": 1})
        docs = await cursor.to_list(length=None)
        out: dict[str, str] = {}
        for d in docs:
            uid = str(d.get("user_id") or "").strip()
            p = str(d.get("persona") or "").strip()
            if uid and p:
                out[uid] = p
        return out

    @staticmethod
    def _persona_dropout_label(persona: str | None) -> int | None:
        if not persona:
            return None
        key = persona.strip().lower()
        if key in HIGH_RISK_PERSONAS:
            return 1
        if key in LOW_RISK_PERSONAS:
            return 0
        return None

    @staticmethod
    def _persona_multiclass_label(persona: str | None) -> int | None:
        if not persona:
            return None
        return PERSONA_TO_INDEX.get(persona.strip().lower())

    @staticmethod
    def _academic_strength(row: dict[str, Any]) -> float:
        """0..1 from scores (0–100 scale) and on-time submission mix."""
        raw = row.get("avg_score_30d")
        score_comp = 0.5
        if raw is not None:
            score_comp = max(0.0, min(1.0, float(raw) / 100.0))
        subs = int(row.get("submissions_total") or 0)
        late = int(row.get("submissions_late") or 0)
        if subs <= 0:
            return score_comp
        on_time_ratio = max(0.0, min(1.0, float(subs - late) / float(subs)))
        return max(0.0, min(1.0, 0.55 * score_comp + 0.45 * on_time_ratio))

    @staticmethod
    def _engagement_strength(row: dict[str, Any]) -> float:
        """0..1 from platform activity (not grades)."""
        logins = min(float(row.get("logins") or 0) / 12.0, 1.0)
        am = min(float(row.get("active_minutes") or 0) / 180.0, 1.0)
        mv = min(float(row.get("materials_viewed") or 0) / 25.0, 1.0)
        mot = min(float(row.get("material_open_time_sec") or 0) / 5000.0, 1.0)
        return max(0.0, min(1.0, (logins + am + mv + mot) / 4.0))

    @staticmethod
    def _attendance_strength(row: dict[str, Any]) -> float:
        ar = row.get("attendance_rate")
        if ar is None:
            return 0.5
        return max(0.0, min(1.0, float(ar)))

    @staticmethod
    def _composite_risk_tier(row: dict[str, Any]) -> int:
        """Ternary dropout-risk tier using multiple dimensions (0=low, 1=medium, 2=high).

        Combines academic, engagement, and attendance. **Strong academics reduce risk** even when
        engagement is low (e.g. good assignment performance with modest app usage → lower tier).

        Note: This is still a function of weekly features — expect strong but not perfect fit under
        time split. For leakage-light archetype learning use ``persona_multiclass``.
        """
        a = TrainingDatasetBuilder._academic_strength(row)
        e = TrainingDatasetBuilder._engagement_strength(row)
        t = TrainingDatasetBuilder._attendance_strength(row)
        streak = int(row.get("inactivity_streak_days") or 0)
        streak_penalty = min(streak / 14.0, 0.32)

        raw_risk = (
            (1.0 - a) * 0.38
            + (1.0 - e) * 0.28
            + (1.0 - t) * 0.24
            + streak_penalty * 0.10
        )
        raw_risk = max(0.0, min(1.0, raw_risk))

        # Compensation: good outcomes on assignments/scores pull risk down (even if app is quiet).
        if a >= 0.72:
            raw_risk *= 0.62
        elif a >= 0.58:
            raw_risk *= 0.78
        if a >= 0.52 and e < 0.38:
            raw_risk *= 0.82
        if a >= 0.68 and t >= 0.55:
            raw_risk *= 0.88

        raw_risk = max(0.0, min(1.0, raw_risk))

        if raw_risk < 0.36:
            return 0
        if raw_risk < 0.58:
            return 1
        return 2

    @staticmethod
    def _to_utc(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def load_weekly_feature_docs(self, since_weeks: int = 12) -> list[dict[str, Any]]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(weeks=since_weeks)
        cursor = self._db["student_weekly_features"].find(
            {"week_start": {"$gte": start, "$lt": end}}
        ).sort([("week_start", 1), ("user_id", 1)])
        return await cursor.to_list(length=None)

    @staticmethod
    def _flatten_features(doc: dict[str, Any]) -> dict[str, Any]:
        features = dict(doc.get("features") or {})
        row = {
            "user_id": str(doc.get("user_id") or ""),
            "course_id": doc.get("course_id"),
            "week_start": doc.get("week_start"),
            "week_end": doc.get("week_end"),
            "source_event_max_time": doc.get("source_event_max_time"),
        }
        for column in FEATURE_COLUMNS:
            row[column] = features.get(column)
        return row

    @staticmethod
    def _proxy_dropout_label(row: dict[str, Any]) -> int:
        """Heuristic label from the same engineered features.

        **Leakage warning:** These rules use `FEATURE_COLUMNS` fields directly, so a tree model
        can trivially reach ~100% accuracy on a random split. Use for sanity checks only, or
        use a time split + disjoint feature policy. Prefer `label_mode='persona'` on simulated data.
        """
        attendance_rate = row.get("attendance_rate")
        inactivity_streak_days = row.get("inactivity_streak_days") or 0
        submissions_late = row.get("submissions_late") or 0
        submissions_total = row.get("submissions_total") or 0
        active_minutes = row.get("active_minutes") or 0.0
        avg_score_30d = row.get("avg_score_30d")

        high_risk = False
        if attendance_rate is not None and attendance_rate < 0.55:
            high_risk = True
        if inactivity_streak_days >= 7:
            high_risk = True
        if submissions_total >= 3 and submissions_late > submissions_total / 2:
            high_risk = True
        if active_minutes < 30 and (avg_score_30d is None or avg_score_30d < 50):
            high_risk = True
        return int(high_risk)

    async def build_training_dataframe(
        self,
        since_weeks: int = 12,
        *,
        label_mode: LabelMode = "composite",
    ) -> TrainingFrame:
        # Normalize legacy CLI alias
        if label_mode == "persona":
            label_mode = "persona_binary"

        label_column = {
            "composite": "label_risk_tier",
            "persona_multiclass": "label_persona_class",
            "persona_binary": "label_dropout_risk",
            "proxy": "label_dropout_proxy",
        }[label_mode]

        docs = await self.load_weekly_feature_docs(since_weeks=since_weeks)
        rows = [self._flatten_features(doc) for doc in docs]

        if not rows:
            return TrainingFrame(rows=[], label_column=label_column, feature_columns=FEATURE_COLUMNS)

        personas: dict[str, str] = {}
        student_ids: set[str] | None = None
        if label_mode != "proxy":
            personas = await self.load_student_personas()
            if personas:
                student_ids = set(personas.keys())

        out_rows: list[dict[str, Any]] = []
        for row in rows:
            uid = str(row.get("user_id") or "").strip()
            if student_ids is not None and uid not in student_ids:
                continue

            if label_mode == "composite":
                row[label_column] = self._composite_risk_tier(row)
            elif label_mode == "persona_multiclass":
                label = self._persona_multiclass_label(personas.get(uid))
                if label is None:
                    continue
                row[label_column] = label
            elif label_mode == "persona_binary":
                label = self._persona_dropout_label(personas.get(uid))
                if label is None:
                    continue
                row[label_column] = label
            else:
                row[label_column] = self._proxy_dropout_label(row)
            out_rows.append(row)

        cleaned_rows, _ = clean_training_rows(out_rows, FEATURE_COLUMNS, label_column)
        return TrainingFrame(rows=cleaned_rows, label_column=label_column, feature_columns=FEATURE_COLUMNS)

    def build_demo_training_dataframe(self, rows: int = 120) -> TrainingFrame:
        rng = random.Random(42)
        records: list[dict[str, Any]] = []
        for index in range(rows):
            attendance_rate = round(rng.uniform(0.2, 0.98), 3)
            inactivity_streak_days = rng.randint(0, 12)
            submissions_total = rng.randint(0, 8)
            submissions_late = rng.randint(0, submissions_total) if submissions_total else 0
            active_minutes = round(rng.uniform(0, 240), 2)
            avg_score_30d = round(rng.uniform(20, 100), 2)
            record = {
                "user_id": f"demo-user-{index % 24}",
                "course_id": 1000 + (index % 6),
                "week_start": datetime.now(timezone.utc) - timedelta(weeks=index % 12),
                "week_end": datetime.now(timezone.utc) - timedelta(weeks=index % 12) + timedelta(days=7),
                "logins": rng.randint(0, 12),
                "active_minutes": active_minutes,
                "materials_viewed": rng.randint(0, 20),
                "material_open_time_sec": round(active_minutes * 60, 2),
                "submissions_total": submissions_total,
                "submissions_on_time": max(submissions_total - submissions_late, 0),
                "submissions_late": submissions_late,
                "attendance_rate": attendance_rate,
                "absence_count": rng.randint(0, 6),
                "inactivity_streak_days": inactivity_streak_days,
                "avg_score_30d": avg_score_30d,
                "score_trend_30d": round(rng.uniform(-18, 18), 2),
                "source_event_max_time": datetime.now(timezone.utc),
            }
            record["label_risk_tier"] = TrainingDatasetBuilder._composite_risk_tier(record)
            records.append(record)

        cleaned_rows, _ = clean_training_rows(records, FEATURE_COLUMNS, "label_risk_tier")
        return TrainingFrame(rows=cleaned_rows, label_column="label_risk_tier", feature_columns=FEATURE_COLUMNS)
