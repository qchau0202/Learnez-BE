#!/usr/bin/env python3
"""Run inference using the trained Dropout Risk model and save results to MongoDB.

Usage:
    python -m ml.training.sample_dropout_predictions
"""

import asyncio
import sys
import joblib
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.database import get_mongo_ai_db
from ml.data.contracts import RiskScoreDocument
from ml.training.dataset_builder import FEATURE_COLUMNS
from ml.training.risk_bands import score_from_probabilities, risk_level_from_score, load_thresholds

async def run_inference():
    ai_db = get_mongo_ai_db()
    model_path = Path(__file__).resolve().parents[1] / "models" / "dropout_rf_composite.joblib"
    threshold_path = Path(__file__).resolve().parents[1] / "models" / "dropout_thresholds_composite.json"

    if not model_path.exists():
        print(f"Error: Model not found at {model_path}. Run training first.")
        return

    print(f"Loading model from {model_path}...")
    model_data = joblib.load(model_path)
    # Some pipelines wrap the model in a {"model": clf, ...} dict.
    clf = model_data["model"] if isinstance(model_data, dict) else model_data

    # Latest weekly snapshot per (user, course).
    cursor = ai_db["student_weekly_features"].find().sort("week_start", -1)
    all_features = await cursor.to_list(length=None)

    if not all_features:
        print("No features found in MongoDB to predict on.")
        return

    latest_map: dict[tuple, dict] = {}
    for f in all_features:
        uid = f.get("user_id")
        cid = f.get("course_id")
        if not uid:
            continue
        key = (uid, cid)
        if key not in latest_map:
            latest_map[key] = f

    records_to_predict = list(latest_map.values())
    print(f"Performing inference for {len(records_to_predict)} student-course pairs...")

    df_input = pd.DataFrame([r["features"] for r in records_to_predict])
    df_input = df_input[FEATURE_COLUMNS].fillna(0)

    probs = clf.predict_proba(df_input.values)
    classes = clf.classes_.tolist()
    low_max, med_max = load_thresholds(threshold_path)

    risk_docs = []
    for i, record in enumerate(records_to_predict):
        proba_by_class = {classes[j]: float(probs[i][j]) for j in range(len(classes))}
        score = score_from_probabilities(proba_by_class)

        # Top-3 features by absolute value — used by the UI to caption the score.
        feat_values = df_input.iloc[i].to_dict()
        sorted_feats = sorted(feat_values.items(), key=lambda x: x[1], reverse=True)
        top_factors = [{"feature": feat, "value": val} for feat, val in sorted_feats[:3]]

        risk_doc = RiskScoreDocument(
            user_id=record.get("user_id"),
            course_id=record.get("course_id"),
            computed_at=datetime.now(timezone.utc),
            model_version="rf_composite_v1",
            risk_score=score,
            risk_level=risk_level_from_score(score, low_max, med_max),
            top_factors=top_factors,
            feature_ref=feat_values,
        )
        risk_docs.append(risk_doc.model_dump())

    for doc in risk_docs:
        await ai_db["risk_scores"].replace_one(
            {"user_id": doc["user_id"], "course_id": doc["course_id"]},
            doc,
            upsert=True,
        )

    print(f"Inference complete. {len(risk_docs)} risk scores updated in MongoDB.")

if __name__ == "__main__":
    asyncio.run(run_inference())