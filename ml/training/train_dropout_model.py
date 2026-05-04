"""CLI entrypoint for the baseline dropout-risk trainer.

This is the practical execution path for the model training data that is built
from MongoDB weekly features.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from .dropout_predictor import DropoutRiskTrainer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train the baseline dropout-risk model from MongoDB features.")
    parser.add_argument("--since-weeks", type=int, default=12, help="Number of recent weeks to use from student_weekly_features.")
    parser.add_argument("--model-path", type=Path, default=None, help="Optional path to save the trained joblib model.")
    parser.add_argument("--demo-data", action="store_true", help="Train on deterministic synthetic data if MongoDB is unavailable.")
    parser.add_argument(
        "--label-mode",
        choices=("composite", "persona_multiclass", "persona_binary", "persona", "proxy"),
        default="composite",
        help="composite (default): 3-way risk from academics+engagement+attendance; strong grades reduce risk even if app use is low. "
        "persona_multiclass: 6-class archetype from simulation_users (best for low leakage vs features). "
        "persona_binary / persona: high/low risk from persona. "
        "proxy: legacy heuristic (trivially learnable from features).",
    )
    parser.add_argument(
        "--split",
        choices=("time", "random"),
        default="time",
        help="time: hold out latest weeks for test. random: stratified shuffle (can be optimistic).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    trainer = DropoutRiskTrainer(model_path=args.model_path)
    result = trainer.train(
        since_weeks=args.since_weeks,
        demo_data=args.demo_data,
        label_mode=args.label_mode,
        split=args.split,
    )

    print(f"trained model saved to: {trainer.model_path}")
    if args.demo_data:
        print("data source: synthetic demo dataset (MongoDB unavailable in this local run)")
    else:
        print("data source: MongoDB student_weekly_features")
        print(f"label_mode: {args.label_mode} | split: {args.split}")
        if args.label_mode == "proxy":
            print("note: proxy mode is a strict feature→label rule — metrics can look artificially strong.")
        if args.label_mode == "composite":
            print(
                "note: composite tier uses multi-factor rules (including academic rescue). "
                "For archetype prediction without feature-derived labels, try --label-mode persona_multiclass.",
            )
    for key, value in result.metrics.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
