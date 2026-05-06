# Training Data for Module 4

The model-training data comes from the MongoDB feature layer, not directly from
raw Supabase tables.

## Data flow

1. Supabase transactional tables provide the source-of-truth LMS records.
2. The ingestion layer converts those rows into MongoDB event documents in the raw database.
3. The weekly feature job reads raw events and writes snapshots to the AI database.
4. The training dataset builder turns weekly snapshots into a pandas table from the AI database.
5. The baseline Random Forest trainer uses that table to fit the first model.
6. The EDA command prints cleaning statistics, label balance, and sample rows.

## Why this is the right training source

- It reduces noise compared with raw event logs.
- It creates stable weekly rows for supervised learning.
- It is easy to backfill and reproduce.
- It can later be swapped from proxy labels to true outcome labels without
  changing the upstream pipeline.

## Two-database layout

This repository now uses two logical MongoDB databases by default:

- raw database: event ingestion and append-only behavioral logs
- ai database: weekly features, risk scores, competency profiles, and training data

If you do not override the environment, they default to:

- `MONGODB_RAW_DB = elearning_raw`
- `MONGODB_AI_DB = elearning`

You can still keep the old `MONGODB_DB` setting for compatibility, but the new
pipeline prefers the split layout so the raw event layer stays separate from the
model-serving layer.

## Current limitation

The repository does not yet contain a clean historical dropout outcome table.
So the first version uses a proxy label derived from weekly risk heuristics:

- low attendance
- long inactivity streak
- late submissions
- very low active minutes with weak scores

This is good enough for a bootstrap baseline, but it should be replaced with a
true labeled outcome when one becomes available.

## EDA command

Run:

```bash
python -m ml.training.eda_report --demo-data
```

or, with live MongoDB configured:

```bash
python -m ml.training.eda_report --since-weeks 12
```

## Sample sync command

If your Supabase service-role credentials are available, you can pull a small
real-data sample and refresh the feature layer with:

```bash
python -m ml.data.supabase_sample_sync --limit-per-table 20
```

Use `--preview-only` if you only want the sample preview without writing to MongoDB.

The EDA output includes:

- row counts before and after cleaning,
- duplicate removal count,
- label balance,
- feature min/max/mean,
- sample cleaned rows ready for inspection before training.

## Data usefulness check (beginner-friendly)

Before training, run:

```bash
python -m ml.data.audit_data_readiness
```

This prints which Mongo collections are ready/empty and whether your AI pipeline
is ready for real-data evaluation.

## Test random students with explanations

After training a model, inspect random student predictions in plain language:

```bash
python -m ml.training.sample_dropout_predictions --sample-size 10
```

## Calibrate risk levels (avoid almost-all HIGH)

Create threshold bands from your current real-user score distribution:

```bash
python -m ml.training.calibrate_dropout_thresholds --since-weeks 20
```

This writes `ml/models/dropout_thresholds_composite.json`, used by:
- `GET /api/analytics/{student_id}/dropout-risk`
- `python -m ml.training.sample_dropout_predictions ...`
