#!/usr/bin/env python3
"""Run train + strict evaluation + smoke prediction for dropout models.

Usage (from BE/, venv on):
  python -m ml.training.run_dropout_pipeline --since-weeks 20 --split group_user
"""

from __future__ import annotations

import argparse
import asyncio
import json
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score

from ml.training.dataset_builder import TrainingDatasetBuilder
from ml.training.dropout_predictor import DropoutRiskTrainer
from ml.training.evaluate_dropout_model import _group_user_split, _normalize_label_mode, _time_split


def _metrics(y_true: list[int], y_pred: list[int], y_proba: Any, n_classes: int) -> dict[str, float]:
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


def _format_metrics_line(metrics: dict[str, float]) -> str:
    keys = ["accuracy", "f1_macro", "roc_auc", "roc_auc_ovr_weighted"]
    return " ".join(f"{k}={metrics[k]:.4f}" for k in keys if k in metrics)


def _evaluate_holdout(
    *,
    label_mode: str,
    split: str,
    since_weeks: int,
    random_state: int,
    model: RandomForestClassifier,
) -> dict[str, Any]:
    builder = TrainingDatasetBuilder()
    frame = asyncio.run(builder.build_training_dataframe(since_weeks=since_weeks, label_mode=label_mode))
    if not frame.rows:
        raise RuntimeError(f"No rows available for label_mode={label_mode}")

    if split == "group_user":
        x_train, x_test, y_train, y_test, train_users, test_users = _group_user_split(
            frame, test_size=0.25, random_state=random_state
        )
        split_detail = {"split": split, "train_users": train_users, "test_users": test_users}
    else:
        x_train, x_test, y_train, y_test = _time_split(frame, test_size=0.25)
        split_detail = {"split": split}

    if not x_test:
        raise RuntimeError(f"Empty test set for label_mode={label_mode}, split={split}")

    n_classes = len(set(y_train) | set(y_test))
    baselines: dict[str, dict[str, float]] = {}
    for name, strategy in [("dummy_majority", "most_frequent"), ("dummy_stratified", "stratified")]:
        dummy = DummyClassifier(strategy=strategy, random_state=random_state)
        dummy.fit(x_train, y_train)
        d_pred = dummy.predict(x_test)
        baselines[name] = _metrics(y_test, d_pred.tolist(), None, n_classes)

    y_pred = model.predict(x_test)
    y_proba = model.predict_proba(x_test) if hasattr(model, "predict_proba") else None
    model_metrics = _metrics(y_test, y_pred.tolist(), y_proba, n_classes)

    sample_count = min(5, len(x_test))
    idx = np.random.RandomState(random_state).choice(len(x_test), size=sample_count, replace=False)
    smoke_predictions: list[dict[str, Any]] = []
    classes = model.classes_.tolist() if hasattr(model, "classes_") else []
    for i in idx.tolist():
        row_out: dict[str, Any] = {
            "test_index": int(i),
            "y_true": int(y_test[i]),
            "y_pred": int(y_pred[i]),
        }
        if y_proba is not None and len(classes) == len(y_proba[i]):
            row_out["proba_by_class"] = {str(classes[j]): float(y_proba[i][j]) for j in range(len(classes))}
        smoke_predictions.append(row_out)

    return {
        "rows": {"train": len(y_train), "test": len(y_test)},
        "split_detail": split_detail,
        "baselines": baselines,
        "model_metrics": model_metrics,
        "smoke_predictions": smoke_predictions,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run train + strict evaluate + smoke prediction and save report.")
    parser.add_argument("--since-weeks", type=int, default=20)
    parser.add_argument(
        "--label-modes",
        type=str,
        default="persona_binary,composite,persona_multiclass",
        help="Comma-separated label modes. Supported: composite, persona_multiclass, persona_binary, persona, proxy",
    )
    parser.add_argument("--split", choices=("group_user", "time"), default="group_user")
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument(
        "--report-json",
        type=Path,
        default=Path("reports/ml/dropout_pipeline_report.json"),
        help="Where to write machine-readable report.",
    )
    parser.add_argument(
        "--report-md",
        type=Path,
        default=Path("reports/ml/dropout_pipeline_report.md"),
        help="Where to write human-readable report.",
    )
    parser.add_argument(
        "--fallback-demo",
        action="store_true",
        help="If a mode has no Mongo rows, train/evaluate that mode on demo synthetic data.",
    )
    parser.add_argument(
        "--check-data-only",
        action="store_true",
        help="Only check Mongo data availability for each label mode (no training).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_modes = [m.strip() for m in args.label_modes.split(",") if m.strip()]
    modes = [_normalize_label_mode(m) for m in raw_modes]
    run_id = uuid.uuid4().hex[:12]

    report: dict[str, Any] = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "since_weeks": args.since_weeks,
        "split": args.split,
        "random_state": args.random_state,
        "status": "running",
        "runs": [],
    }

    md_lines = [
        "# Dropout ML Pipeline Report",
        "",
        f"- run_id: {run_id}",
        f"- generated_at_utc: {report['generated_at_utc']}",
        f"- since_weeks: {args.since_weeks}",
        f"- split: {args.split}",
        "- status: running",
        "",
    ]

    def _write_reports(status: str) -> None:
        report["status"] = status
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
        finalized_lines = list(md_lines)
        for idx, line in enumerate(finalized_lines):
            if line.startswith("- status: "):
                finalized_lines[idx] = f"- status: {status}"
                break
        args.report_md.parent.mkdir(parents=True, exist_ok=True)
        args.report_md.write_text("\n".join(finalized_lines), encoding="utf-8")

    success_count = 0
    for mode in modes:
        mode_started = datetime.now(timezone.utc)
        model_path = Path(__file__).resolve().parents[1] / "models" / f"dropout_rf_{mode}.joblib"
        trainer = DropoutRiskTrainer(model_path=model_path)
        demo_used = False
        mode_status = "ok"
        mode_error: str | None = None
        mode_trace: str | None = None
        data_source = "REAL_MONGO"

        if args.check_data_only:
            try:
                frame = asyncio.run(
                    TrainingDatasetBuilder().build_training_dataframe(
                        since_weeks=args.since_weeks,
                        label_mode=mode,
                    )
                )
                row_count = len(frame.rows)
                mode_status = "data_ready" if row_count > 0 else "no_data"
                if row_count == 0:
                    mode_error = "No rows from Mongo for this label mode."
                run_entry = {
                    "label_mode": mode,
                    "model_path": str(model_path),
                    "status": mode_status,
                    "data_source": data_source,
                    "started_at_utc": mode_started.isoformat(),
                    "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                    "duration_sec": round((datetime.now(timezone.utc) - mode_started).total_seconds(), 3),
                    "data_check": {"row_count": row_count, "label_column": frame.label_column},
                    "error": mode_error,
                }
                report["runs"].append(run_entry)
                print(f"[{mode}] data_source={data_source} status={mode_status} rows={row_count}")
                if mode_error:
                    print(f"[{mode}] note: {mode_error}")
                md_lines.extend(
                    [
                        f"## {mode}",
                        "",
                        f"- data_source: {data_source}",
                        f"- status: {mode_status}",
                        f"- row_count: {row_count}",
                        f"- label_column: {frame.label_column}",
                        "",
                    ]
                )
                if row_count > 0:
                    success_count += 1
                _write_reports(status="running")
                continue
            except Exception as err:
                mode_status = "error"
                mode_error = str(err)
                mode_trace = traceback.format_exc()
                run_entry = {
                    "label_mode": mode,
                    "model_path": str(model_path),
                    "status": mode_status,
                    "data_source": data_source,
                    "started_at_utc": mode_started.isoformat(),
                    "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                    "duration_sec": round((datetime.now(timezone.utc) - mode_started).total_seconds(), 3),
                    "error": mode_error,
                    "error_traceback": mode_trace,
                }
                report["runs"].append(run_entry)
                print(f"[{mode}] data_source={data_source} status=error")
                print(f"[{mode}] error: {mode_error}")
                md_lines.extend([f"## {mode}", "", f"- data_source: {data_source}", "- status: error", f"- error: {mode_error}", ""])
                _write_reports(status="running")
                continue

        try:
            train_result = trainer.train(
                since_weeks=args.since_weeks,
                label_mode=mode,
                split="time",
                random_state=args.random_state,
            )
            loaded_model = trainer.load()
            eval_result = _evaluate_holdout(
                label_mode=mode,
                split=args.split,
                since_weeks=args.since_weeks,
                random_state=args.random_state,
                model=loaded_model,
            )
        except RuntimeError as err:
            if not args.fallback_demo:
                mode_status = "error"
                mode_error = str(err)
                mode_trace = traceback.format_exc()
                run_entry = {
                    "label_mode": mode,
                    "model_path": str(model_path),
                    "error": str(err),
                    "status": mode_status,
                    "started_at_utc": mode_started.isoformat(),
                    "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                    "duration_sec": round((datetime.now(timezone.utc) - mode_started).total_seconds(), 3),
                    "error_traceback": mode_trace,
                }
                report["runs"].append(run_entry)
                print(f"[{mode}] skipped: {err}")
                md_lines.extend([f"## {mode}", "", f"- error: {err}", ""])
                _write_reports(status="running")
                continue

            demo_used = True
            train_result = trainer.train(
                since_weeks=args.since_weeks,
                label_mode=mode,
                split="time",
                random_state=args.random_state,
                demo_data=True,
            )
            loaded_model = trainer.load()
            frame = trainer.dataset_builder.build_demo_training_dataframe()
            x_train, x_test, y_train, y_test = _time_split(frame, test_size=0.25)
            y_pred = loaded_model.predict(x_test)
            y_proba = loaded_model.predict_proba(x_test) if hasattr(loaded_model, "predict_proba") else None
            n_classes = len(set(y_train) | set(y_test))
            eval_result = {
                "rows": {"train": len(y_train), "test": len(y_test)},
                "split_detail": {"split": "time_demo"},
                "baselines": {},
                "model_metrics": _metrics(y_test, y_pred.tolist(), y_proba, n_classes),
                "smoke_predictions": [],
            }
            mode_status = "ok_demo_fallback"
            mode_error = str(err)
            mode_trace = traceback.format_exc()
            data_source = "DEMO_FALLBACK"

        mode_finished = datetime.now(timezone.utc)
        duration_sec = round((mode_finished - mode_started).total_seconds(), 3)

        run_entry = {
            "label_mode": mode,
            "model_path": str(model_path),
            "status": mode_status,
            "data_source": data_source,
            "started_at_utc": mode_started.isoformat(),
            "finished_at_utc": mode_finished.isoformat(),
            "duration_sec": duration_sec,
            "demo_data_used": demo_used,
            "fallback_reason": mode_error,
            "fallback_traceback": mode_trace,
            "train_metrics": train_result.metrics,
            "evaluation": eval_result,
        }
        report["runs"].append(run_entry)
        success_count += 1

        print(f"[{mode}] model: {model_path}")
        print(f"[{mode}] data_source={data_source}")
        if demo_used:
            print(f"[{mode}] note: Mongo rows unavailable, used demo synthetic data.")
        print(f"[{mode}] status={mode_status} duration_sec={duration_sec}")
        print(f"[{mode}] holdout metrics: {_format_metrics_line(eval_result['model_metrics'])}")
        if eval_result["baselines"]:
            print(
                f"[{mode}] baselines: majority({_format_metrics_line(eval_result['baselines']['dummy_majority'])}) "
                f"stratified({_format_metrics_line(eval_result['baselines']['dummy_stratified'])})"
            )
        print()

        md_lines.extend(
            [
                f"## {mode}",
                "",
                f"- model_path: `{model_path}`",
                f"- data_source: {data_source}",
                f"- status: {mode_status}",
                f"- duration_sec: {duration_sec}",
                f"- rows: train={eval_result['rows']['train']} test={eval_result['rows']['test']}",
                f"- model_metrics: {_format_metrics_line(eval_result['model_metrics'])}",
                f"- demo_data_used: {demo_used}",
                "- smoke_predictions:",
            ]
        )
        if mode_error:
            md_lines.append(f"- fallback_reason: {mode_error}")
        if eval_result["baselines"]:
            md_lines.append(f"- baseline_majority: {_format_metrics_line(eval_result['baselines']['dummy_majority'])}")
            md_lines.append(f"- baseline_stratified: {_format_metrics_line(eval_result['baselines']['dummy_stratified'])}")
        for p in eval_result["smoke_predictions"]:
            md_lines.append(
                f"  - idx={p['test_index']} true={p['y_true']} pred={p['y_pred']} "
                f"proba={json.dumps(p.get('proba_by_class', {}), ensure_ascii=True)}"
            )
        md_lines.append("")
        _write_reports(status="running")

    final_status = "completed" if success_count > 0 else "failed"
    _write_reports(status=final_status)

    print(f"Saved JSON report: {args.report_json}")
    print(f"Saved Markdown report: {args.report_md}")
    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
