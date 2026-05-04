# Machine learning pipeline and chat / agent integration plan

This document ties together **dataset work in `elearning_raw`**, the **feature layer** (`student_weekly_features` in the AI DB), **model training**, **evaluation**, and **product integration** (analytics chat + simple agentic actions).

Your raw collections (from seeding and Compass schemas):

- `activity_events` — `event_type`, `event_time`, `user_id`, `course_id`, `properties` (e.g. `page`, `persona`, `room`)
- `assessment_events` — `event_type`, `timing_label`, `final_score`, `assignment_id`, `submission_id`, `properties.due_at`, `persona`
- `attendance_events` — `event_type`, `status` (Present / Late / Absent), `course_id`, `properties.session_date`
- `simulation_users` — `user_id`, `role_id`, `persona`, `department_id`, `student_id` / `lecturer_id`, GPA fields for students

---

## Phase 1 — Understanding the data (EDA)

**Goals:** Volume, time coverage, distributions, join keys, leakage risks.

1. Run the automated report:
   - `python -m ml.eda.generate_eda_report --out ./reports/elearning_raw_eda.md`
2. Manually inspect in Compass or Notebooks:
   - **Per-persona** slices (join `simulation_users.persona` on `user_id`) for activity, submission timing, attendance.
   - **Lecturer vs student** rows (role_id in `simulation_users`; event streams may mix `graded` events with lecturer `user_id`).
3. Record decisions:
   - Canonical **timestamp** for modeling: `event_time` (UTC).
   - Which **event types** feed which features (already reflected in `ml/data/feature_jobs.py`; extend if you add new `event_type` values).

**Deliverable:** EDA Markdown + short “data dictionary” addendum (field → meaning → used in features yes/no).

---

## Phase 2 — Dataset preparation

**Labels (important):**

- **`--label-mode composite` (default):** Ternary risk **0 / 1 / 2** from **three dimensions** — academic strength (scores + on-time work), **engagement** (logins, time-on-platform, materials), **attendance**, plus a small inactivity-streak term. **Strong academics explicitly reduce** the tier even when engagement is low (students who deliver good work but rarely open the app are **not** forced into “high dropout risk” by engagement alone).
- **`--label-mode persona_multiclass`:** **Six classes** (`star` … `dormant`) from `simulation_users.persona`. The persona was fixed at simulation time, so it is **not** a deterministic function of that week’s feature vector — **best** for checking that the model learns behavior without label tautology.
- **`--label-mode persona_binary` / `persona`:** Two-class high/low persona groups.
- **`proxy`:** Legacy rule; still **almost identical** to the feature columns → expect inflated metrics.

Always prefer **`--split time`** for evaluation. **Production:** replace with **real outcomes** (withdraw, fail, intervention) when available.

**Goals:** Turn raw events into **model-ready tables** without duplicating or leaking future information.

1. **Raw → weekly features** (existing path):
   - `python -m ml.data.backfill_weekly_features --use-raw-range`
   - Output: `student_weekly_features` in the **AI Mongo DB** (`MONGODB_AI_DB` / default), one row per `(user_id, week_start)` with a `features` dict.
2. **Optional: course-level or multi-table datasets**
   - If you need per-course risk, extend the aggregator to emit `(user_id, course_id, week_start)` rows or a separate `course_engagement_features` collection (see `contracts.py` / design docs).
3. **Labels**
   - **Today:** proxy label in `dataset_builder._proxy_dropout_label` (attendance, lateness, activity). Document limitations.
   - **Target:** replace with **real outcomes** (drop/withdraw, final fail, intervention flag) from Supabase when available; store label table with `user_id`, `course_id`, `outcome`, `as_of`.
4. **Train/validation/test split**
   - **Time-based split** by `week_start` (e.g. train on weeks 1–N, validate N+1…, test holdout last segment) to avoid leakage.
   - Optionally **group by `user_id`** so the same user does not appear in both train and test if rows are highly correlated.

**Deliverable:** Versioned **dataset manifest** (Mongo query or export path, date range, row counts, label definition version).

---

## Phase 3 — Dataset analysis (beyond EDA)

**Goals:** Confirm the feature space supports the product use cases (chat + risk).

1. **Feature distributions** on weekly snapshots (histograms / correlation with proxy label).
2. **Stability** across weeks (concept drift): compare early vs late weeks in the simulation window.
3. **Fairness / cohort checks** (optional): by `department_id` or `faculty_id` from `simulation_users` join — watch for tiny groups.
4. **Baseline models**
   - Keep **Random Forest** baseline (`train_dropout_model.py`).
   - Add **logistic regression** or **gradient boosting** for comparison; track **ROC-AUC**, **PR-AUC**, **calibration** (reliability diagram) if you expose probabilities in the UI.

**Deliverable:** Short model comparison note + chosen **production candidate** and **probability calibration** policy (e.g. Platt scaling if needed).

---

## Phase 4 — Training

1. **Hyperparameters:** grid or small Bayesian search on validation window only.
2. **Artifacts:** persist `joblib` (or ONNX later) under `BE/ml/models/` with **metadata** JSON: `model_version`, `feature_schema_version`, `trained_at`, `data_window`.
3. **Reproducibility:** pin `random_state`, log git commit and dataset manifest id.

**Deliverable:** Trained model + metadata sidecar.

---

## Phase 5 — Evaluation

1. **Offline:** held-out time slice; classification report, ROC, confusion matrix; **stratify by persona** in simulation to see if the model learns signal vs artifact.
2. **Error analysis:** high-FP / high-FN users — inspect raw week leading up to prediction (events + attendance + submissions).
3. **Online (later):** shadow mode — log predictions without affecting UX; compare to lecturer actions or outcomes after the term.

**Deliverable:** Evaluation report + thresholds for `low/medium/high` risk bands.

---

## Phase 6 — Integration into the app

### 6.1 Inference service

- Add a small **FastAPI** module (e.g. `app/api/activity/inference.py`) that:
  - Loads the latest model + feature column order at startup (or on configurable path).
  - Accepts `user_id` (+ optional `course_id`, `week_start`) and **reads precomputed features** from Mongo (or computes on the fly from raw for debugging only).
  - Returns `{ risk_score, risk_level, top_factors }` aligned with `RiskScoreDocument` in `contracts.py`.
- **Auth:** student sees **self** only; lecturer/admin see students in their courses (reuse existing IAM checks).

### 6.2 Persistence

- Write predictions to `risk_scores` (or `competency_profiles`) in the AI DB for audit and chat context.

### 6.3 Frontend

- Replace mock analytics where appropriate: **Risk** tab and **Behavior** tab call the new APIs.
- **Analytics chat** (`Analytics.tsx`): today uses `chatResponses` mock — replace with:
  - `POST /api/analytics/chat` (or `/api/activity/chat`) sending message + `user_id` context.
  - Backend builds a **short context pack**: last weekly features, recent risk, course list (from Supabase), **no PII** in prompts beyond what policy allows.

---

## Phase 7 — Chat + simple agentic actions

**Goals:** Conversational UX with **bounded tools** (safe, auditable).

1. **Chat orchestration**
   - LLM reads user message + structured context (features, risk, deadlines from Supabase if needed).
   - System prompt: cite only allowed facts; if uncertain, say so.

2. **Agentic tools (examples)**

   | Tool | Effect | Guardrails |
   |------|--------|------------|
   | `get_my_risk_summary` | Read-only; returns latest risk + drivers | Student: self only |
   | `get_course_engagement` | Aggregated stats for a course | Role + enrollment check |
   | `create_reminder_notification` | Calls existing notifications API | Confirm intent; rate limit; template text |
   | `suggest_learning_resources` | Returns links to materials / modules | No auto-enroll |

3. **Implementation pattern**
   - Tool definitions (JSON schema) → LLM function calling or structured output → **server executes** tools (never client-side secrets).
   - Log each turn in `chat_events` / `ai_action_events` (per `MONGODB_AI_DATA_DESIGN.md`) with `event_time`, `approval` if needed.

4. **Safety**
   - Lecturer approval for actions that **notify other users** or **change grades**.
   - Redact raw emails in logs used for model context where possible.

**Deliverable:** Chat API + 2–3 tools in v1; expand after review.

---

## Suggested timeline (indicative)

| Week | Focus |
|------|--------|
| 1 | EDA report + data dictionary + time-based split design |
| 2 | Weekly features stable; first trained baseline + offline metrics |
| 3 | Inference API + Risk tab wired; shadow logging |
| 4 | Chat backend + read-only tools; replace mock chat |
| 5+ | Real labels, more tools, online eval |

---

## References in this repo

- Raw event contracts: `BE/ml/data/contracts.py`
- Weekly aggregation: `BE/ml/data/feature_jobs.py`
- Training frame: `BE/ml/training/dataset_builder.py`, `dropout_predictor.py`
- EDA generator: `BE/ml/eda/generate_eda_report.py`
- Design notes: `MONGODB_AI_DATA_DESIGN.md`, `BE/ml/data/README_MONGODB_FOUNDATION.md`
