"""Sample real LMS data from Supabase and sync it into MongoDB.

This command is intentionally small and safe: it limits the number of records
per source table, normalizes them into event documents, writes to the raw event
database, and then refreshes the AI feature layer.

Use this when you want to inspect real data paths and verify the end-to-end
pipeline before training.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from .feature_jobs import WeeklyFeatureAggregator
from .ingestion import EventNormalizer, Module4IngestionPlan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sample real Supabase records and sync them into MongoDB.")
    parser.add_argument("--limit-per-table", type=int, default=20, help="Maximum rows to fetch from each Supabase table.")
    parser.add_argument("--preview-only", action="store_true", help="Do not write to MongoDB; just print a sample preview.")
    parser.add_argument("--save-json", type=Path, default=None, help="Optional JSON file to write the preview/report to.")
    parser.add_argument(
        "--student-ids",
        type=str,
        default="",
        help="Comma-separated student user_id list for targeted sync (Supabase -> Mongo).",
    )
    return parser.parse_args()


async def sync_live_sample(limit_per_table: int, preview_only: bool, student_ids: list[str] | None = None) -> dict[str, object]:
    plan = Module4IngestionPlan()
    normalizer = EventNormalizer()
    if student_ids:
        sample_bundle = plan.reader.fetch_student_scoped_bundle(student_ids)
    else:
        sample_bundle = plan.reader.fetch_sample_bundle(limit_per_table=limit_per_table)
    normalized = normalizer.normalize_source_bundle(sample_bundle)

    event_times: list[datetime] = []
    for docs in normalized.values():
        for doc in docs:
            event_time = doc.get("event_time")
            if isinstance(event_time, str):
                parsed = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
                event_times.append(parsed.astimezone(timezone.utc))
            elif isinstance(event_time, datetime):
                event_times.append(event_time.astimezone(timezone.utc) if event_time.tzinfo else event_time.replace(tzinfo=timezone.utc))

    write_counts: dict[str, int] = {name: len(docs) for name, docs in normalized.items()}
    if not preview_only:
        for collection_name, docs in normalized.items():
            if docs:
                await plan.writer.insert_events(collection_name, docs)
        aggregator = WeeklyFeatureAggregator()
        if event_times:
            refreshed = await aggregator.persist_weekly_snapshots_for_range(min(event_times), max(event_times))
        else:
            refreshed = 0
    else:
        refreshed = 0

    preview = {
        collection: docs[:3]
        for collection, docs in normalized.items()
        if docs
    }
    return {
        "write_counts": write_counts,
        "refreshed_weekly_snapshots": refreshed,
        "preview": preview,
    }


def main() -> int:
    args = parse_args()
    student_ids = [s.strip() for s in args.student_ids.split(",") if s.strip()]
    report = asyncio.run(sync_live_sample(args.limit_per_table, args.preview_only, student_ids=student_ids))
    print(f"write_counts: {report['write_counts']}")
    print(f"refreshed_weekly_snapshots: {report['refreshed_weekly_snapshots']}")
    print("preview:")
    print(json.dumps(report["preview"], indent=2, default=str, ensure_ascii=False))

    if args.save_json:
        args.save_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"saved_json: {args.save_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
