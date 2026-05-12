"""Training utilities for the dropout-risk model.

Modules (each runs as a CLI via ``python -m ml.training.<module>``):

* ``dataset_builder``               — feature/label assembly from Mongo.
* ``dropout_predictor``             — ``DropoutRiskTrainer`` + train CLI.
* ``evaluate_dropout_model``        — strict (group_user / time) holdout eval + baselines.
* ``calibrate_dropout_thresholds``  — derive low/medium/high cutoffs from real scores.
* ``run_dropout_pipeline``          — train + evaluate + smoke-predict, write reports.
* ``sample_dropout_predictions``    — run inference for every student, upsert ``risk_scores``.
* ``eda_report``                    — read-only feature data quality + label balance.
* ``risk_bands``                    — shared probability → 0..1 score + band helpers.
"""

from .dataset_builder import TrainingDatasetBuilder, TrainingFrame
from .dropout_predictor import DropoutRiskTrainer

__all__ = ["TrainingDatasetBuilder", "TrainingFrame", "DropoutRiskTrainer"]
