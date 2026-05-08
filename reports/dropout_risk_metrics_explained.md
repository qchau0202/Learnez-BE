# Dropout-risk metrics: what we actually analyse

This document is the source of truth for what is computed, where the numbers
come from, and which collections are real vs aspirational.

The system has three storage layers:

1. **Supabase (Postgres)** — the transactional source of truth for users,
   courses, enrollments, assignments, attendance.
2. **MongoDB `elearning_raw`** — append-only behavioural events. Originally
   seeded by simulation; new events are written by the live LMS.
3. **MongoDB `learnez_ai`** — derived AI artefacts (features, predictions,
   plans). The model trains on this layer, never on raw events directly.

```
Supabase (truth)        →  raw events (`elearning_raw`)        [production path]
                              ↓
                    weekly aggregator job
                              ↓
            features (`learnez_ai.student_weekly_features`)    ←──── demo seeders
                              ↓                                       (write here directly,
                       model + thresholds                              skipping raw events)
                              ↓
              risk scores (`learnez_ai.risk_scores`)  ──→  /api/analytics/*
```

> **Demo mode.** In a clean development install the `elearning_raw`
> collections are intentionally left empty — see §6 "Operational pipeline"
> for the demo seeding shortcut. Production deployments still write to
> `elearning_raw` from `POST /api/activity/log`; the aggregator path remains
> the canonical one.

---

## 1. Features used by the model

All twelve features below come from `student_weekly_features.features` and are
the inputs to the dropout RandomForest. The same column order is exported from
`ml/training/dataset_builder.py:FEATURE_COLUMNS`.

| Column                     | Meaning                                                        | How it is computed                                                                       |
|----------------------------|----------------------------------------------------------------|------------------------------------------------------------------------------------------|
| `logins`                   | # of `login` events that week                                  | Count of `activity_events.event_type == "login"` in the week range                       |
| `active_minutes`           | Total minutes the student was active                           | Sum of `duration_sec / 60` over `material_open` + `session_heartbeat` events             |
| `materials_viewed`         | Distinct material/page opens                                   | Count of `material_open` (LMS) and `page_view` (simulated) events                         |
| `material_open_time_sec`   | Total time spent reading/watching                              | Sum of `material_open.duration_sec`                                                       |
| `submissions_total`        | Assignments handed in                                          | Count of `assessment_events.event_type == "submission_created"` (or legacy shapes)        |
| `submissions_on_time`      | Subset of above with `timing_label != "late"`                  | Same source                                                                               |
| `submissions_late`         | Subset with `timing_label == "late"`                           | Same source                                                                               |
| `attendance_rate`          | Hits / possible attendance opportunities                       | From `attendance_events` (`session_attended` / `session_absent`)                          |
| `absence_count`            | Absences this week                                             | `attendance_events` where status is `absent`                                              |
| `inactivity_streak_days`   | Days since the latest event                                    | Set by trend-builder; `0` from the seed data                                              |
| `avg_score_30d`            | Mean assignment grade (rescaled to 0–100 if source is 0–10)    | From `assessment_events.final_score` / `submission.final_score`                           |
| `score_trend_30d`          | Slope vs previous window                                       | Computed during long-window aggregation (often `null` until enough history)               |

These are aggregated **per (user, course, week_start)** so a student in three
courses produces three feature rows per week.

---

## 2. From features to a risk decision (composite mode)

The composite labeller (`TrainingDatasetBuilder._composite_risk_tier`) blends
three normalised dimensions plus an inactivity penalty:

```text
academic_strength    = 0.55 * (avg_score_30d / 100) + 0.45 * (on-time submission ratio)
engagement_strength  = mean( logins/12, active_min/180, materials_viewed/25, mot/5000 )
attendance_strength  = attendance_rate                                      ← clipped to [0,1]
streak_penalty       = min(inactivity_streak_days / 14, 0.32)

raw_risk =  (1 - academic)    * 0.38
          + (1 - engagement)  * 0.28
          + (1 - attendance)  * 0.24
          + streak_penalty    * 0.10
```

**Academic rescue.** Strong assignment performance multiplicatively reduces
risk so that a quiet but high-scoring student is not flagged as high-risk:

- `academic ≥ 0.72`  →  `risk *= 0.62`
- `academic ≥ 0.58`  →  `risk *= 0.78`
- `academic ≥ 0.52` AND `engagement < 0.38`  →  `risk *= 0.82`
- `academic ≥ 0.68` AND `attendance ≥ 0.55`  →  `risk *= 0.88`

The result is bucketed into a 3-tier label used as the training target:

| Risk tier  | `raw_risk` band   |
|------------|-------------------|
| 0 — low    | `< 0.36`          |
| 1 — medium | `0.36 ≤ x < 0.58` |
| 2 — high   | `≥ 0.58`          |

The trained classifier predicts probabilities for those tiers; we collapse
them into a continuous `risk_score ∈ [0,1]` (see `risk_bands.py`) and look up
the **calibrated thresholds** (`dropout_thresholds_composite.json`) to map
back to `low / medium / high`. That is what the dashboard shows.

---

## 3. What the dashboard shows, and where each number comes from

| UI element                           | Source                                                                        |
|--------------------------------------|-------------------------------------------------------------------------------|
| Faculty/department overview chart    | `/api/analytics/by-faculty` — aggregates `risk_scores` (or features fallback) joined with Supabase `student_profiles → departments → faculties` |
| Risk distribution pie                | `/api/analytics/overview.risk_distribution` — same pipeline, filtered by faculty/department/course |
| Risk-by-course bar                   | Client-side group-by on the returned `StudentRiskCard` list                   |
| At-risk students table               | `/api/analytics/students` — one card per (student, course); enriched with `users.full_name`, `student_profiles.student_id`, `class`, faculty/department |
| “Source” badge                       | `data_source` field — `risk_scores` if precomputed, `weekly_features_fallback` if the API ran the model live, `empty` if there is no data yet |
| Reload data                          | Re-issues the same call; useful after a backfill                              |
| Filters (Faculty / Department / Course) | Sent to the BE; faculty/department are resolved against `student_profiles` to keep client mocks out of the picture |

---

## 4. Reliability of `elearning_raw`

Only the four collections in the EDA report (`elearning_raw_eda.md`) hold
real volume:

- `activity_events` — ~67k docs, 1,072 distinct users, full week 2026-01..2026-06.
- `assessment_events` — ~16k docs with `final_score` populated.
- `attendance_events` — ~28k docs with status mix Present/Late/Absent.
- `simulation_users` — 1,080 personas (43 lecturers, 1,037 students).

Two cautions:

1. The volume is dominated by **simulated** persona-driven sessions, not
   production users. That is fine for training, but real students will
   produce a different distribution (e.g. fewer `page_view` heartbeats).
2. Until the front-end actually emits `material_open` / `submission_created`
   events to Mongo, only `activity_events.login` and the assignment hooks in
   `BE/app/services/notifications/scenario_notifications.py` will keep the
   feature numbers fresh. Everything else stops growing.

---

## 5. Other collections shown in the screenshot

These were planned during the AI design phase. The current state is:

| Collection                     | Layer        | Status        | Used by                       | Verdict                                                                 |
|--------------------------------|--------------|---------------|-------------------------------|-------------------------------------------------------------------------|
| `student_weekly_features`      | `learnez_ai` | ~61k docs     | training, calibration, scoring| **Required**. This is the single feature input.                          |
| `risk_scores`                  | `learnez_ai` | populated by `/sample_dropout_predictions` | `/analytics/overview`, `/students`, `/by-faculty` | **Required**. Cached predictions; lets the API answer in <50 ms.       |
| `student_daily_features`       | `learnez_ai` | empty         | not wired                     | Optional. Daily resolution helps EWS but doubles storage. Defer.        |
| `course_engagement_features`   | `learnez_ai` | empty         | not wired                     | Optional. Course-level rollups for instructor analytics. Defer.         |
| `competency_profiles`          | `learnez_ai` | computed on demand | `/analytics/{id}/competency`  | **Useful**. Generated live from weekly features; do not pre-store yet.  |
| `learning_paths`               | `learnez_ai` | computed on demand | `/analytics/{id}/learning-path`| **Useful** in the same way; live computation is sufficient.            |
| `recommendation_explanations`  | `learnez_ai` | empty         | reserved for chat agent       | Defer until LLM agent is enabled.                                       |
| `agent_runs` / `chat_events` / `ai_action_events` | `learnez_ai` | empty | reserved for chat/agent telemetry | Telemetry only; no value for dropout model.                             |
| `simulation_users`             | `elearning_raw` | 1,080 docs | training labels (persona modes) | Required only while we evaluate persona-supervised baselines.          |
| `activity_events` / `assessment_events` / `attendance_events` / `content_events` | `elearning_raw` | populated except `content_events` | input to the weekly aggregator | Required. `content_events` is empty — feature falls back gracefully.    |

**Recommendation.** Keep `student_weekly_features` and `risk_scores` as the
only must-grow collections. Treat the rest as on-demand or telemetry; do not
spend time pre-populating them.

---

## 6. Operational pipeline (how the numbers refresh)

### 6a. Production path (real student activity)

1. `python -m BE.ml.data.backfill_weekly_features --weeks 12`
   — replay `elearning_raw` into `student_weekly_features`.
2. `python -m BE.ml.training.train_dropout_model --label-mode composite`
   — fit the model.
3. `python -m BE.ml.training.calibrate_dropout_thresholds`
   — write `dropout_thresholds_composite.json` (low/medium/high cutoffs).
4. `python -m BE.ml.training.sample_dropout_predictions`
   — score all current students and upsert into `risk_scores`.
5. The API reads `risk_scores` first, falls back to running the model on
   `student_weekly_features` if step 4 has not run yet.

The Admin dashboard surfaces the data-source label so you can tell which
path served the answer.

### 6b. Demo / thesis-mode path (no raw events required)

`elearning_raw` is intentionally not seeded for demo runs — generating
millions of synthetic events just to re-aggregate them is wasteful when we
can write the aggregate features directly. Use the cohort orchestrator
instead:

```bash
cd BE
python -m ml.data.seed_demo_cohort \
  --weeks 16 --ignore-course-window \
  --pin "student1@email.com:at_risk,student2@email.com:thriving" \
  --train --score
```

What it touches:

| Layer / collection                            | Written by                         | Purpose                                                        |
|-----------------------------------------------|-------------------------------------|----------------------------------------------------------------|
| `learnez_ai.student_weekly_features`          | `seed_demo_student.py`              | drives engagement charts, model training, ML risk inference    |
| `public.course_attendance`                    | `seed_demo_attendance.py`           | populates lecturer attendance dashboards & student check-in log|
| `public.assignment_submissions` / `_answers`  | `seed_demo_submissions.py`          | populates grade-distribution chart & lecturer "graded" tab     |
| `learnez_ai.risk_scores`                      | `sample_dropout_predictions.py`     | cached predictions read by `/api/analytics/*`                  |

Personas (`thriving` / `steady` / `at_risk`) are deterministically assigned
from a hash of `user_id`, so the same student keeps the same fingerprint
across re-runs. `--pin email:persona` overrides specific accounts so the
demo always has a guaranteed at-risk learner to walk through.

All three seeders are idempotent:

* weekly features are upserted by `(user_id, course_id, week_start)`;
* attendance only inserts new `(student_id, course_id, session_date)` triples;
* submissions skip any `(student_id, assignment_id)` that already exists
  (we never mutate or delete graded work).

`elearning_raw.activity_events` & friends remain empty. That is fine — the
model only reads `student_weekly_features`. If you ever want to validate
the production aggregator against demo data, run
`backfill_weekly_features` against a separate Mongo instance instead of
mixing real and synthetic events.

---

## 7. What I would *not* trust today

- **`absence_count` from simulation** — under-counts because the simulator
  emits `Late` and `Present` more than `Absent`. Use `attendance_rate`
  directly.
- **`score_trend_30d`** — frequently null until at least three weeks of
  scores are present.
- **`inactivity_streak_days` for old weeks** — fixed at 0 in the historical
  backfill; only the live aggregator updates it correctly.
- **Department-level numbers when most students share `department_id = 1`** —
  the dropout model has no opinion about departments; the per-faculty chart
  will look uniform until you have meaningful enrolment spread.
