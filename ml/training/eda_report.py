"""
EDA report for Module 4 training data.
This command is meant to be run before training so you can inspect the cleaned
weekly feature rows, missingness, label balance, and a few sample records.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from .dataset_builder import FEATURE_COLUMNS, TrainingDatasetBuilder, clean_training_rows, summarize_training_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect cleaned training data from MongoDB or demo rows.")
    parser.add_argument("--since-weeks", type=int, default=12, help="How many weeks of Mongo feature snapshots to inspect.")
    parser.add_argument("--demo-data", action="store_true", help="Inspect deterministic demo rows instead of MongoDB data.")
    parser.add_argument("--sample-size", type=int, default=5, help="Number of cleaned rows to print as sample data.")
    parser.add_argument("--save-json", type=Path, default=None, help="Optional path to write the EDA report as JSON.")
    return parser.parse_args()


def print_report(summary, cleaning, sample_rows) -> None:
    print(f"rows_in: {cleaning.input_rows}")
    print(f"rows_out: {cleaning.output_rows}")
    print(f"duplicate_rows_removed: {cleaning.duplicate_rows_removed}")
    print(f"rows_missing_identity: {cleaning.rows_missing_identity}")
    print(f"rows_missing_label: {cleaning.rows_missing_label}")
    print(f"unique_users: {summary.user_count}")
    print(f"unique_courses: {summary.course_count}")
    print(f"label_counts: {summary.label_counts}")
    print("feature_stats:")
    for name in FEATURE_COLUMNS:
        stats = summary.feature_stats.get(name, {})
        print(f"  {name}: min={stats.get('min', 0.0):.2f} max={stats.get('max', 0.0):.2f} mean={stats.get('mean', 0.0):.2f}")
    print("sample_rows:")
    for row in sample_rows:
        print(json.dumps(row, default=str, ensure_ascii=False))


def main() -> int:
    args = parse_args()
    builder = TrainingDatasetBuilder()

    if args.demo_data:
        frame = builder.build_demo_training_dataframe(rows=max(args.sample_size * 20, 120))
    else:
        import asyncio

        frame = asyncio.run(builder.build_training_dataframe(since_weeks=args.since_weeks))

    cleaned_rows, cleaning = clean_training_rows(frame.rows, frame.feature_columns, frame.label_column)
    summary = summarize_training_rows(cleaned_rows, frame.feature_columns, frame.label_column)
    sample_rows = cleaned_rows[: args.sample_size]

    print_report(summary, cleaning, sample_rows)

    if args.save_json:
        payload = {
            "summary": {
                "rows_in": cleaning.input_rows,
                "rows_out": cleaning.output_rows,
                "duplicate_rows_removed": cleaning.duplicate_rows_removed,
                "rows_missing_identity": cleaning.rows_missing_identity,
                "rows_missing_label": cleaning.rows_missing_label,
                "unique_users": summary.user_count,
                "unique_courses": summary.course_count,
                "label_counts": summary.label_counts,
                "feature_stats": summary.feature_stats,
            },
            "sample_rows": sample_rows,
        }
        args.save_json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"saved_json: {args.save_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
