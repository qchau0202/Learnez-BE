# Learnez-BE

FastAPI backend for an e-learning LMS with an integrated ML pipeline
(weekly feature aggregation, dropout-risk model, learning-path
recommender) and an agentic AI chatbot.

## Tech stack

- Python 3.11+
- FastAPI + Uvicorn
- MongoDB (via `motor`) — raw event lake + AI artefacts
- Supabase (Postgres) — transactional LMS data
- scikit-learn, pandas, numpy, joblib — dropout-risk model
- Pluggable LLM providers (Stub / Gemini / OpenAI / OpenRouter)

## Project layout

```
BE/
├── app/                       # FastAPI application
│   ├── api/                   # Routers split by domain
│   │   ├── activity/          # activity, analytics, chat, simulation
│   │   ├── assessment/        # assignments, grading, attendance, notifications
│   │   ├── course/            # courses, content, enrollment
│   │   ├── iam/               # auth, accounts, RBAC
│   │   ├── search/, storage/  # ...
│   │   └── router.py          # Top-level /api mount
│   ├── core/                  # config, database, security, supabase cache
│   ├── models/                # Pydantic models
│   ├── services/
│   │   ├── ai/
│   │   │   └── llm.py         # << single source of truth for LLM providers
│   │   ├── assessment/, course/, iam/, …
│   │   └── activity/, notifications/, storage/
│   └── middlewares/
├── ml/
│   ├── data/                  # Data layer (contracts, seeders, feature jobs)
│   │   ├── curriculum/        # TDTU portal crawler + course catalog sync
│   │   │   ├── catalog.py     # Master course list
│   │   │   ├── crawler.py     # Authenticated TDTU syllabus scraper
│   │   │   ├── schedule.py    # Deterministic scheduling generator
│   │   │   ├── sync.py        # Upsert crawled syllabi into Supabase
│   │   │   └── seed.py        # Crawl + sync orchestrator
│   │   ├── mongo/             # MongoDB-side bootstrap
│   │   │   └── bootstrap.py   # Collections + indexes for raw + AI DBs
│   │   ├── students/          # Student-side seeders + diagnostics
│   │   │   ├── provision.py   # Create ~50 IT student accounts
│   │   │   ├── content.py     # Modules + assignments + questions per course
│   │   │   ├── features.py    # Weekly student_weekly_features
│   │   │   ├── attendance.py  # Per-student course_attendance rows
│   │   │   ├── submissions.py # Per-student graded submissions
│   │   │   ├── cohort.py      # Bulk uniform seeder for all students
│   │   │   ├── behaviour.py   # Mongo risk_scores + competency + raw events
│   │   │   ├── student1.py    # Orchestrator for the primary demo account
│   │   │   └── diagnose.py    # Read-only audit of one student's data
│   │   ├── contracts.py       # Shared Pydantic contracts
│   │   ├── feature_jobs.py    # Weekly feature aggregator (used live + by training)
│   │   └── syllabi.json       # Crawler output cache (gitignored)
│   ├── training/              # Dropout-risk model training pipeline
│   │   ├── dataset_builder.py            # Mongo features → sklearn frame
│   │   ├── dropout_predictor.py          # Trainer class + train CLI
│   │   ├── evaluate_dropout_model.py     # Strict holdout + baselines
│   │   ├── calibrate_dropout_thresholds.py
│   │   ├── run_dropout_pipeline.py       # Full pipeline orchestrator
│   │   ├── sample_dropout_predictions.py # Run inference, upsert risk_scores
│   │   ├── eda_report.py                 # Feature data quality report
│   │   └── risk_bands.py                 # Shared score/band helpers
│   └── models/                # Trained model artefacts (.joblib)
├── sql/                       # One-off SQL scripts (RBAC reset)
├── test/                      # Pytest / shell-driven integration tests
├── reports/ml/                # Auto-generated training reports
├── main.py                    # FastAPI entrypoint
├── render.yaml, runtime.txt   # Deployment config
└── requirements.txt
```

## Environment variables

Copy `.env.example` to `.env` and fill in:

```env
MONGODB_URI=...                 # primary mongo cluster URI
MONGODB_DB=learnez              # main DB
SUPABASE_URL=...
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...   # required by seeders
JWT_SECRET=...
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=60
```

Optional LLM config (single file: `app/services/ai/llm.py`):

```env
LLM_PROVIDER=stub               # stub | gemini | openai | openrouter
LLM_MODEL=                      # provider-specific default applies if blank
LLM_FALLBACK_PROVIDER=          # optional; same value space as above
LLM_FALLBACK_MODEL=

# Provider keys — only the ones you use are read
GEMINI_API_KEY=
OPENAI_API_KEY=
OPENROUTER_API_KEY=

# OpenRouter cascade (free-tier friendly)
OPENROUTER_MODELS=openai/gpt-oss-120b:free,meta-llama/llama-3.3-70b-instruct:free
OPENROUTER_FALLBACK_MODELS=
OPENROUTER_SITE_URL=
OPENROUTER_SITE_TITLE=
```

Default is `stub` — fully offline, deterministic, no API key needed
(the chatbot still works, with rule-based intent matching).

## Install + run

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install -r requirements.txt
uvicorn main:app --reload
```

Default endpoints:

- API root: `http://127.0.0.1:8000/`
- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`

## Seeding pipeline (one-shot, idempotent)

Every step below is **safe to re-run** — the seeders short-circuit
on existing rows. Run them in order on a fresh database. All commands
assume the current working directory is `BE/`.

```bash
# 1. Bootstrap MongoDB collections + indexes (raw + AI databases)
python -m ml.data.mongo.bootstrap

# 2. Crawl the TDTU syllabus portal + upsert into Supabase
#    (uses ml/data/syllabi.json as a cache so re-runs are fast)
python -m ml.data.curriculum.seed

# 3. Create ~50 IT student accounts (idempotent on email)
python -m ml.data.students.provision

# 4. Provision modules + assignments + questions per course
#    (8 modules per course, mix of MCQ / mixed / essay / manual)
python -m ml.data.students.content

# 5. Seed the comprehensive demo profile for student1
#    (enrolments + features + attendance + graded submissions
#     + Mongo behaviour + risk-score history + competency profiles)
python -m ml.data.students.student1

# 6. (optional) Seed a broader cohort with mixed personas
python -m ml.data.students.cohort

# 7. (optional) Audit student1's data state (read-only)
python -m ml.data.students.diagnose
```

## ML training pipeline

After seeding, train the dropout-risk model end-to-end:

```bash
# Full pipeline: dataset build → train → evaluate → calibrate
#                → sample predictions report
python -m ml.training.run_dropout_pipeline
```

Per-stage entry points (useful for iteration):

```bash
python -m ml.training.dropout_predictor          # train only
python -m ml.training.evaluate_dropout_model     # strict eval + baselines
python -m ml.training.calibrate_dropout_thresholds
python -m ml.training.sample_dropout_predictions # run inference + upsert risk_scores
python -m ml.training.eda_report                 # feature data quality + label balance
```

Trained artefacts land in `ml/models/`. The runtime predictor at
`app/services/ai/dropout_predictor.py` picks them up automatically.

## Database layout

- **Supabase (Postgres)** — users, roles, courses, modules, assignments,
  submissions, enrolments, attendance.
- **MongoDB**:
  - `learnez_ai.student_weekly_features` — weekly per-student snapshots
    (feature vectors for the risk model).
  - `learnez_ai.risk_scores` — historical risk predictions.
  - `learnez_ai.competency_profiles` — per-subject competency snapshots.
  - `learnez_ai.learning_paths` / `learnez_ai.student_path_picks` —
    AI-built and student-picked path artefacts.
  - `learnez_ai.agent_runs` — chat session metadata.
  - `learnez_ai.learning_path_intake_sessions` — wizard intake state
    (TTL-indexed).
  - `elearning_raw.activity_events`, `content_events`,
    `attendance_events`, `assessment_events` — raw behavioural lake.
  - `elearning_raw.chat_events` / `elearning_raw.ai_action_events` —
    chat turn log + tool-call audit.

## API overview

The application mounts a versioned API router at `/api`:

- `/api/auth/*`, `/api/accounts/*` — authentication, account
  management
- `/api/courses/*`, `/api/content/*`, `/api/enrollment/*` — courses
- `/api/attendance/*`, `/api/assignments/*`, `/api/grading/*`,
  `/api/notifications/*` — assessment surfaces
- `/api/activity/*`, `/api/analytics/*` — activity + analytics
- `/api/chat/*` — agentic AI chat (sessions, messages, suggestions,
  learning-path conversation)
- `/api/storage/*` — file handling

See `/docs` (Swagger UI) for the complete contract.

## LLM provider configuration

All LLM logic lives in **one file**: `app/services/ai/llm.py`. It
contains:

- The provider-agnostic types (`ChatProvider`, `ChatMessage`,
  `ToolCall`, `ToolDefinition`, `ChatResponse`).
- Four concrete providers — `StubProvider` (offline default),
  `GeminiProvider`, `OpenAIProvider`, `OpenRouterProvider`.
- The factory functions `get_provider()` and
  `get_fallback_provider()` that the chat router calls.

To switch providers, set `LLM_PROVIDER` in your `.env`. No code
changes required.
