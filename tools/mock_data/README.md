# Learnez simulation seed (MongoDB + optional API)

Generates **synthetic students and lecturers** (no admin simulation) and writes **batched** documents to MongoDB raw collections used by the ML pipeline, aligned with `learnez-supabase-db.csv` (courses, departments, faculties, schedules).

## Conventions (from your Supabase export)

- **Courses**: `class_room` like `A101`, `B101`, `course_session_date` weekday names, `from_department` links to departments.
- **Students**: cohort class `25k50201` / `25b50201` / `25f50201`; majors match department names; GPA ranges depend on **persona**.
- **Lecturers**: `LecturerK1234`, `LecturerB1234`, `LecturerF1234` style codes.
- **No `module_materials` interaction**: engagement is simulated with `**page_view`** and `**session_heartbeat`** on course routes (not `material_open`), so you do not need real material rows for believable timelines.

## Personas (student behavior)


| Persona      | Meaning                                                                                               |
| ------------ | ----------------------------------------------------------------------------------------------------- |
| `star`       | Strong engagement: many logins/page views, mostly present, early / on-time submissions, higher scores |
| `steady`     | Typical “good” student: on-time work, solid attendance                                                |
| `uneven`     | Mixed late/on-time, uneven attendance                                                                 |
| `struggling` | Frequent late/missing work, weaker attendance and scores                                              |
| `sparse`     | Rare activity; few sessions and submissions                                                           |
| `dormant`    | Almost no interaction; may skip most courses                                                          |


Lecturers get persona `lecturer`: logins, roster `**page_view`s**, in-class **heartbeats** (uses `class_room` from the course when present).

## Install

```bash
cd BE/tools/mock_data
npm install
```

## Mongo mode (default)

Uses `MONGO_URI` and `MONGODB_RAW_DB` (default `elearning_raw`). Upserts by `idempotency_key` in batches (default 400) to limit memory.

`**MONGO_URI` must be a real connection string** starting with `mongodb://` or `mongodb+srv://`.  
Using the literal placeholder `export MONGO_URI="..."` will fail with `Invalid scheme`.

```bash
export MONGO_URI="mongodb://localhost:27017"
# or e.g. mongodb+srv://USER:PASS@cluster.mongodb.net/
export MONGODB_RAW_DB="elearning_raw"
node simulation_seed.js --users 1000 --since-weeks 16 --batch 400 --mode mongo
```

**CSV path**: defaults to `Source/learnez-supabase-db.csv` (three levels above this folder). Override with `--csv` or `SEED_CSV_PATH`.

**Lecturer share**: default 4% of `--users` are lecturers (`--lecturer-fraction 0.04`).

## API mode + clock-warp

Backend endpoint: `**POST /api/activity/sim/ingest-batch`** (public path, but requires secret).

1. Set a strong secret in the API environment:
  `ML_SIMULATION_SECRET=...`
2. Run the seed in API mode:

```bash
export ML_SIMULATION_SECRET="your-secret"
node simulation_seed.js \
  --users 1000 \
  --since-weeks 16 \
  --batch 300 \
  --mode api \
  --api-url http://127.0.0.1:8000/api/activity/sim/ingest-batch \
  --api-secret "$ML_SIMULATION_SECRET"
```

Headers sent per batch:

- `X-Simulation-Secret`: must match `ML_SIMULATION_SECRET`
- `X-Clock-Warp: 1` — server sets `created_at` equal to each document’s `event_time` so backfilled activity lines up for feature jobs

Optional `**X-Event-Time**` on the request applies only to documents that omit `event_time` (the script always sets `event_time` per document).

## Collections written

- `simulation_users` — reference rows (`persona`, `student_id` / `lecturer_id`, department, email) for joins and labeling
- `activity_events` — `login`, `page_view`, `session_heartbeat`
- `attendance_events` — `session_attended` / `session_absent` with `Present` / `Late` / `Absent` in `status`
- `assessment_events` — `submission_created` and sometimes `graded` (lecturer user_id)

## Legacy script

`seeder.js` is an older minimal generator. Prefer `**simulation_seed.js**` for persona coverage and CSV-aware enrollments.

## Next steps for ML training (fastest path)

From the `**BE**` folder with the same **venv** as the API (`pip install -r requirements.txt`):

1. **Build weekly feature rows** (matches your simulation window):
  ```bash
   python -m ml.data.backfill_weekly_features --use-raw-range
  ```
   Or only the last N weeks:
2. **Train the baseline dropout model** from `student_weekly_features`:
  ```bash
   python -m ml.training.train_dropout_model --since-weeks 20 --model-path ./models/dropout_baseline.joblib
  ```

The aggregator was updated so simulated `**submission_created**`, `**session_attended` / `session_absent**`, `**page_view**`, and `**session_heartbeat**` events feed real feature counts; **0–10 grades** are scaled to **0–100** in `avg_score_30d` for the training proxy.