#!/usr/bin/env python3
"""Honest evaluation for the dropout / risk RF — stricter splits + baselines.

Time-split still lets the *same student* appear in train and test on different weeks, so the
model can partly memorize user-level habits. **group_user** holdout puts entire users in test
only — metrics usually drop; that is a more realistic generalization check.

Usage (from BE/, venv on):

  python -m ml.training.evaluate_dropout_model --since-weeks 20 --label-mode persona_binary --split group_user
  python -m ml.training.evaluate_dropout_model --since-weeks 20 --label-mode composite --split group_user

Also runs **dummy baselines** on the same split (majority class, stratified random).
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

import numpy as np

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split

from ml.training.dataset_builder import TrainingDatasetBuilder, TrainingFrame
from ml.training.dropout_predictor import DropoutRiskTrainer


def _normalize_label_mode(mode: str) -> str:
    if mode == "persona":
        return "persona_binary"
    return mode


def _group_user_split(
    frame: TrainingFrame,
    *,
    test_size: float = 0.25,
    random_state: int = 42,
) -> tuple[list[list[float]], list[list[float]], list[int], list[int], int, int]:
    rows = frame.rows
    users = sorted({str(r.get("user_id") or "").strip() for r in rows if r.get("user_id")})
    if len(users) < 8:
        print(
            f"Warning: only {len(users)} distinct user_id(s); group_user split falls back to random row split (not user-level)."
        )
        x, y = DropoutRiskTrainer._prepare_xy(frame)
        x_tr, x_te, y_tr, y_te = train_test_split(
            x, y, test_size=test_size, random_state=random_state, stratify=y if len(set(y)) > 1 else None
        )
        return x_tr, x_te, y_tr, y_te, len(users), max(1, len(users) // 4)

    rng = random.Random(random_state)
    ucopy = users[:]
    rng.shuffle(ucopy)
    n_test_users = max(1, int(len(ucopy) * test_size))
    test_users = set(ucopy[:n_test_users])
    train_rows = [r for r in rows if str(r.get("user_id") or "").strip() not in test_users]
    test_rows = [r for r in rows if str(r.get("user_id") or "").strip() in test_users]
    x_train, y_train = DropoutRiskTrainer._prepare_xy_static(train_rows, frame.feature_columns, frame.label_column)
    x_test, y_test = DropoutRiskTrainer._prepare_xy_static(test_rows, frame.feature_columns, frame.label_column)
    n_train_u = len({str(r.get("user_id")) for r in train_rows})
    n_test_u = len({str(r.get("user_id")) for r in test_rows})
    return x_train, x_test, y_train, y_test, n_train_u, n_test_u


def _time_split(frame: TrainingFrame, test_size: float = 0.25) -> tuple[list, list, list, list]:
    x_train, x_test, y_train, y_test = DropoutRiskTrainer._time_based_split(frame, test_size=test_size)
    if len(x_test) == 0 or len(x_train) == 0:
        x, y = DropoutRiskTrainer._prepare_xy(frame)
        return train_test_split(
            x, y, test_size=test_size, random_state=42, stratify=y if len(set(y)) > 1 else None
        )
    return x_train, x_test, y_train, y_test


def _metrics_block(y_true: list[int], y_pred: list[int], y_proba, n_classes: int) -> dict[str, float]:
    out: dict[str, float] = {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "f1_macro": float(f1_score(y_true, y_pred, average="macro", zero_division=0)),
    }
    if y_proba is not None and len(set(y_true)) > 1:
        try:
            if n_classes > 2:
                out["roc_auc_ovr_weighted"] = float(
                    roc_auc_score(y_true, y_proba, multi_class="ovr", average="weighted")
                )
            else:
                out["roc_auc"] = float(roc_auc_score(y_true, y_proba[:, 1]))
        except ValueError:
            pass
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Evaluate RF with user-level or time holdout + baselines.")
    p.add_argument("--since-weeks", type=int, default=20)
    p.add_argument(
        "--label-mode",
        choices=("composite", "persona_multiclass", "persona_binary", "persona", "proxy"),
        default="persona_binary",
        help="Should match how you intend to use the model in production.",
    )
    p.add_argument(
        "--split",
        choices=("time", "group_user"),
        default="group_user",
        help="group_user: hold out entire students (stricter). time: hold out latest weeks.",
    )
    p.add_argument("--random-state", type=int, default=42)
    args = p.parse_args()

    label_mode = _normalize_label_mode(args.label_mode)
    builder = TrainingDatasetBuilder()
    frame = asyncio.run(
        builder.build_training_dataframe(since_weeks=args.since_weeks, label_mode=label_mode)
    )
    if not frame.rows:
        print("No rows — check Mongo and label_mode.")
        return 1

    if args.split == "group_user":
        x_train, x_test, y_train, y_test, n_tr_u, n_te_u = _group_user_split(
            frame, test_size=0.25, random_state=args.random_state
        )
        print(f"Split: group_user | train users ≈ {n_tr_u} | test users ≈ {n_te_u}")
    else:
        x_train, x_test, y_train, y_test = _time_split(frame, test_size=0.25)
        print("Split: time (latest weeks in test)")

    print(f"Rows: train={len(y_train)} test={len(y_test)} | label={frame.label_column}")
    print()

    # Baselines (same test set)
    for name, strat in [("dummy_majority", "most_frequent"), ("dummy_stratified", "stratified")]:
        d = DummyClassifier(strategy=strat, random_state=args.random_state)
        d.fit(x_train, y_train)
        p_pred = d.predict(x_test)
        m = _metrics_block(y_test, p_pred, None, len(set(y_train) | set(y_test)))
        print(f"{name}: accuracy={m['accuracy']:.4f} f1_macro={m['f1_macro']:.4f}")

    rf = RandomForestClassifier(
        n_estimators=200,
        max_depth=8,
        min_samples_leaf=3,
        random_state=args.random_state,
        class_weight="balanced",
    )
    rf.fit(x_train, y_train)
    y_pred = rf.predict(x_test)
    y_proba = rf.predict_proba(x_test)
    n_cls = len(rf.classes_)
    m = _metrics_block(y_test, y_pred, y_proba, n_cls)
    print()
    print("random_forest:", " ".join(f"{k}={v:.4f}" for k, v in m.items()))
    print()
    print("confusion_matrix (rows=true, cols=pred):")
    labels = sorted(set(y_train) | set(y_test))
    print(confusion_matrix(y_test, y_pred, labels=labels))
    print()
    print(classification_report(y_test, y_pred, labels=labels, zero_division=0))

    # Label shuffle sanity (should collapse toward dummy)
    y_train_shuf = np.array(y_train)
    rng = np.random.RandomState(args.random_state)
    rng.shuffle(y_train_shuf)
    rf_s = RandomForestClassifier(
        n_estimators=200,
        max_depth=8,
        min_samples_leaf=3,
        random_state=args.random_state + 1,
        class_weight="balanced",
    )
    rf_s.fit(x_train, y_train_shuf.tolist())
    y_pred_s = rf_s.predict(x_test)
    m_s = _metrics_block(y_test, y_pred_s, None, n_cls)
    print("sanity_check (shuffled train labels):", " ".join(f"{k}={v:.4f}" for k, v in m_s.items()))
    print("  → If RF still scores high here, the split may be leaking or features encode label.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
