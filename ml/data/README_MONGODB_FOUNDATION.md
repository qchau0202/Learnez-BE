# MongoDB AI Data Foundation

This module prepares the data layer for Module 4: Activity Tracking & Data Collection.
The goal is to store high-frequency behavioral data in MongoDB, turn it into stable
feature snapshots, and then feed those snapshots into risk prediction and learning
path models.

## What to store

### Event layer

Store immutable behavioral events with a common contract:

- `activity_events`: login, logout, page view, material open/close, heartbeats
- `assessment_events`: submission created, submission updated, graded, finalized
- `content_events`: material views, downloads, document engagement, content actions
- `attendance_events`: status changes, session attendance, lateness, absence notes
- `chat_events`: chatbot conversations and context turns
- `ai_action_events`: tool calls, approved actions, denied actions, action audit trail

Why this matters:

- It gives a complete timeline for each user.
- It keeps the raw history immutable for later backfill and debugging.
- It lets the same event stream support multiple models later.

### Feature layer

Store rolling snapshots built from the event layer:

- `student_daily_features`
- `student_weekly_features`
- `course_engagement_features`

Typical features:

- login frequency
- active minutes
- material engagement time
- submission timing patterns
- attendance rate
- inactivity streaks
- grade momentum

Why this matters:

- Models work better on compact, normalized features than on raw logs.
- Weekly snapshots make training easier and reduce noise.
- Feature snapshots are reusable for dashboards and chatbot context.

### Decision layer

Store AI outputs and explanations:

- `competency_profiles`
- `risk_scores`
- `learning_paths`
- `recommendation_explanations`
- `agent_runs`

Why this matters:

- Students and lecturers need the result, not just the score.
- Versioned outputs make the system auditable.
- Explanations support lecturer trust and intervention decisions.

## Supabase sources to read from

Use Supabase as the source of truth for transactional LMS data:

- `users`
- `courses`
- `modules`
- `module_materials`
- `assignments`
- `assignment_questions`
- `assignment_submissions`
- `assignment_submission_answers`
- `course_attendance`
- `course_enrollments`
- `notifications`

How they map:

- `users` provides the student/lecturer identity key.
- `courses`, `modules`, and `assignments` provide academic context.
- `assignment_submissions` and `assignment_submission_answers` support performance labels.
- `course_attendance` supports engagement and dropout features.
- `module_materials` and `notifications` support content interaction tracking.

## Recommended initial MongoDB structure

Use one MongoDB cluster per environment first (`dev`, `staging`, `prod`).
Keep these layers separate by collection naming:

- event collections are append-only
- feature collections are upserted snapshots
- decision collections are versioned outputs

## Initial implementation order

1. Bootstrap collections and indexes.
2. Add event emitters for login, content usage, submission timing, grading, attendance, and chatbot actions.
3. Build daily and weekly aggregation jobs.
4. Train a baseline Random Forest risk model from weekly features.
5. Persist risk scores, competency summaries, and learning paths with version metadata.
6. Add chatbot action auditing and approval flow.

## Quick real-student provisioning (Supabase -> Mongo)

For a fast demo bootstrap with 10-15 real student accounts in Supabase and
automatic mapping into Mongo analytics:

```bash
python -m ml.data.provision_real_students --count 12
```

This command:
- creates student auth/users/profiles in Supabase,
- seeds enrollment + attendance + submission records,
- syncs those students into Mongo raw events,
- refreshes weekly feature snapshots.

## Why this foundation is important

- It prevents raw LMS tables from becoming the AI runtime store.
- It makes model training repeatable because the same source events always produce the same feature windows.
- It supports future agentic AI safely because every action can be audited and explained.
