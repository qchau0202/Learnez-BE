"""Baseline dropout-risk training using the weekly MongoDB feature layer.

The first model is intentionally simple: a Random Forest trained on weekly
feature snapshots with a proxy label. This gives the project a working baseline
before a true dropout outcome label is available.
"""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
from pathlib import Path
from typing import Literal

import joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.model_selection import train_test_split

from .dataset_builder import FEATURE_COLUMNS, TrainingDatasetBuilder, TrainingFrame, clean_training_rows


@dataclass(slots=True)
class TrainResult:
    model: RandomForestClassifier
    metrics: dict[str, float | str]
    feature_columns: list[str]


class DropoutRiskTrainer:
    """Train and persist the first dropout-risk classifier."""

    def __init__(self, model_path: str | Path | None = None) -> None:
        self.dataset_builder = TrainingDatasetBuilder()
        self.model_path = Path(model_path or Path(__file__).resolve().parents[1] / "models" / "dropout_rf.joblib")
        self.model_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _prepare_xy(frame: TrainingFrame) -> tuple[list[list[float]], list[int]]:
        features, target = DropoutRiskTrainer._prepare_xy_static(
            frame.rows, frame.feature_columns, frame.label_column
        )
        if not features:
            raise RuntimeError("No weekly feature rows available for training")
        return features, target

    @staticmethod
    def _time_based_split(
        frame: TrainingFrame,
        *,
        test_size: float = 0.25,
    ) -> tuple[list[list[float]], list[list[float]], list[int], list[int]]:
        """Hold out the latest calendar weeks for test (reduces leakage across correlated weekly rows)."""
        rows = frame.rows
        label_col = frame.label_column
        feat_cols = frame.feature_columns
        weeks = sorted({r.get("week_start") for r in rows if r.get("week_start") is not None})
        if len(weeks) < 3:
            return [], [], [], []

        n_test = max(1, int(len(weeks) * test_size))
        test_weeks = set(weeks[-n_test:])
        train_rows = [r for r in rows if r.get("week_start") not in test_weeks]
        test_rows = [r for r in rows if r.get("week_start") in test_weeks]
        x_train, y_train = DropoutRiskTrainer._prepare_xy_static(train_rows, feat_cols, label_col)
        x_test, y_test = DropoutRiskTrainer._prepare_xy_static(test_rows, feat_cols, label_col)
        return x_train, x_test, y_train, y_test

    @staticmethod
    def _prepare_xy_static(
        rows: list[dict],
        feature_columns: list[str],
        label_column: str,
    ) -> tuple[list[list[float]], list[int]]:
        cleaned_rows, _ = clean_training_rows(rows, feature_columns, label_column)
        features: list[list[float]] = []
        target: list[int] = []
        for row in cleaned_rows:
            label = row.get(label_column)
            if label is None:
                continue
            target.append(int(label))
            features.append([float(row.get(column) or 0.0) for column in feature_columns])
        return features, target

    def train(
        self,
        since_weeks: int = 12,
        random_state: int = 42,
        demo_data: bool = False,
        *,
        label_mode: Literal[
            "composite", "persona_multiclass", "persona_binary", "persona", "proxy"
        ] = "composite",
        split: Literal["time", "random"] = "time",
    ) -> TrainResult:
        if label_mode == "persona":
            label_mode = "persona_binary"

        if demo_data:
            frame = self.dataset_builder.build_demo_training_dataframe()
            label_mode = "composite"
        else:
            frame = asyncio.run(
                self.dataset_builder.build_training_dataframe(since_weeks=since_weeks, label_mode=label_mode)
            )
        if not frame.rows:
            raise RuntimeError("No training rows after filtering; check Mongo feature data and label_mode.")

        if split == "time":
            x_train, x_test, y_train, y_test = self._time_based_split(frame, test_size=0.25)
            if len(x_test) == 0 or len(x_train) == 0:
                x, y = self._prepare_xy(frame)
                x_train, x_test, y_train, y_test = train_test_split(
                    x,
                    y,
                    test_size=0.25,
                    random_state=random_state,
                    stratify=y if len(set(y)) > 1 else None,
                )
        else:
            x, y = self._prepare_xy(frame)
            x_train, x_test, y_train, y_test = train_test_split(
                x,
                y,
                test_size=0.25,
                random_state=random_state,
                stratify=y if len(set(y)) > 1 else None,
            )

        model = RandomForestClassifier(
            n_estimators=200,
            max_depth=8,
            min_samples_leaf=3,
            random_state=random_state,
            class_weight="balanced",
        )
        model.fit(x_train, y_train)

        y_pred = model.predict(x_test)
        y_prob_full = model.predict_proba(x_test) if hasattr(model, "predict_proba") else None

        metrics: dict[str, float | str] = {
            "classification_report": classification_report(y_test, y_pred, zero_division=0),
        }
        if y_prob_full is not None:
            n_classes = len(model.classes_)
            try:
                if n_classes > 2 and len(set(y_test)) > 1:
                    metrics["roc_auc_ovr_weighted"] = float(
                        roc_auc_score(y_test, y_prob_full, multi_class="ovr", average="weighted")
                    )
                elif n_classes == 2 and len(set(y_test)) > 1:
                    metrics["roc_auc"] = float(roc_auc_score(y_test, y_prob_full[:, 1]))
            except ValueError:
                pass

        metrics["label_mode"] = label_mode
        metrics["split"] = split

        self.save(model)
        return TrainResult(model=model, metrics=metrics, feature_columns=FEATURE_COLUMNS)

    def save(self, model: RandomForestClassifier) -> None:
        joblib.dump(model, self.model_path)

    def load(self) -> RandomForestClassifier:
        return joblib.load(self.model_path)
