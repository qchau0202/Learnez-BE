# Extraction Plan for MongoDB AI Data

This document defines the first extraction pass from Supabase into MongoDB.
It is the bridge between the LMS transactional schema and the AI data layer.

## Auth strategy

Use Supabase service-role auth for backend ingestion jobs.

Why:

- the job runs server-side, not in the browser,
- it needs read access across multiple tables,
- it should not depend on a specific end-user token,
- it needs to backfill data for many users consistently.

Use a user token only when the extraction is intentionally scoped to the
current actor, such as a lecturer dashboard or a user-facing debug view.

## Initial extraction order

1. `users`
2. `courses`
3. `modules`
4. `module_materials`
5. `assignments`
6. `assignment_submissions`
7. `assignment_submission_answers`
8. `course_attendance`
9. `course_enrollments`
10. `notifications`

## Suggested table-to-collection mapping

### `users`

Use for identity resolution.

- feed `user_id`, role, and account state into all downstream docs
- use `role_id` / role name to scope student, lecturer, and admin behavior

### `courses`

Use for course context.

- attach `course_id` and lecturer ownership to events
- group behavior by subject, semester, academic year, and department

### `modules`

Use for finer-grained learning context.

- connect material views and assignment behavior to a specific module
- support course progression and engagement analysis

### `module_materials`

Use for content engagement.

- track open/close/view/download patterns
- estimate content attention and study intensity

### `assignments`

Use for assessment windows and labels.

- identify due dates, hard due dates, and grading state
- define on-time vs late submission logic

### `assignment_submissions`

Use for performance and timing signals.

- create submission lifecycle events
- record score, correction state, and lateness

### `assignment_submission_answers`

Use for competency and item-level analysis.

- measure correctness per question
- identify weak topics and recurring error patterns

### `course_attendance`

Use for dropout risk and engagement.

- compute attendance rate over 7/30 day windows
- detect inactivity and presence instability

### `course_enrollments`

Use for cohort membership.

- restrict feature aggregation to active course enrollment windows
- support course-level rollups and recommendation context

### `notifications`

Use for platform nudges and chatbot context.

- detect reminder delivery and announcement exposure
- measure whether nudges correlate with re-engagement

## First-pass event contracts

### `activity_events`

Store:

- login, logout
- page views
- material open / close
- session heartbeats

Used for:

- active time
- study frequency
- content interaction patterns

### `assessment_events`

Store:

- submission created
- submission updated
- graded
- graded finalized

Used for:

- timing labels: early / on time / late
- score trajectories
- grading milestones

### `attendance_events`

Store:

- attendance status changes
- session attendance records

Used for:

- attendance rate
- lateness and absence trend
- dropout risk input

### `content_events`

Store:

- material open / close
- content view duration
- downloads and resource access

Used for:

- engagement score
- active minutes
- content affinity

## Feature layer inputs

The first training-ready snapshot should be weekly.

Suggested fields:

- `logins`
- `active_minutes`
- `materials_viewed`
- `material_open_time_sec`
- `submissions_total`
- `submissions_on_time`
- `submissions_late`
- `attendance_rate`
- `absence_count`
- `inactivity_streak_days`
- `avg_score_30d`
- `score_trend_30d`

Why weekly:

- it smooths daily noise,
- it aligns with LMS reporting cycles,
- it gives a stable training grain for the first Random Forest model.

## Risk score outputs

Persist these fields in `risk_scores`:

- `risk_score` between 0 and 1
- `risk_level` as `low`, `medium`, or `high`
- `model_version`
- `computed_at`
- `feature_ref`
- `top_factors`

Why:

- explains the result,
- supports versioned retraining,
- lets lecturers verify why a student was flagged.

## Recommended first implementation cut

1. Build the Supabase extraction reader.
2. Emit immutable `activity_events`, `assessment_events`, `attendance_events`, and `content_events`.
3. Generate weekly feature snapshots.
4. Write the first `risk_scores` docs.
5. Add explanation and learning-path outputs only after the core risk model is stable.
