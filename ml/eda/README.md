# EDA for `elearning_raw`

Generates a **Markdown** report from live MongoDB data (counts, time ranges, categorical distributions, basic quality checks).

## Prerequisites

- `BE` venv with `pymongo` installed (`pip install -r requirements.txt`)
- `MONGO_URI` set to a valid `mongodb://` or `mongodb+srv://` string
- `MONGODB_RAW_DB` if not using the default `elearning_raw`

## Run

From the `BE` directory:

```bash
python -m ml.eda.generate_eda_report --out ./reports/elearning_raw_eda.md
```

Print to stdout instead:

```bash
python -m ml.eda.generate_eda_report
```

## Collections covered

Aligned with the seeding script and your Compass JSON schemas:

| Collection | Role |
|------------|------|
| `activity_events` | Logins, page views, heartbeats |
| `assessment_events` | Submissions, grades |
| `attendance_events` | Present / Late / Absent |
| `simulation_users` | Persona, role, department (reference for joins) |

For the full ML + chat integration roadmap, see `ML_PIPELINE_AND_CHAT_PLAN.md` in this folder.

## End-to-end model validation pipeline

To train and validate multiple label modes in one run (with strict holdout and prediction smoke checks):

```bash
python -m ml.training.run_dropout_pipeline --since-weeks 20 --split group_user
```

Outputs:
- `reports/ml/dropout_pipeline_report.json`
- `reports/ml/dropout_pipeline_report.md`
