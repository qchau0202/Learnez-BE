"""Backfill `student_weekly_features` in the AI Mongo DB from raw event collections.

Run from the `BE` directory (with venv activated):

    python -m ml.data.backfill_weekly_features --since-weeks 20

After seeding simulation data, prefer `--use-raw-range` to cover the full seeded span:

    python -m ml.data.backfill_weekly_features --use-raw-range

Then train:

    python -m ml.training.train_dropout_model --since-weeks 20
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ml.data.feature_jobs import WeeklyFeatureAggregator, utc_now
else:
    from .feature_jobs import WeeklyFeatureAggregator, utc_now

from app.core.database import get_mongo_raw_db


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _raw_event_time_bounds() -> tuple[datetime, datetime] | None:
    raw = get_mongo_raw_db()
    mins: list[datetime] = []
    maxs: list[datetime] = []
    for name in ("activity_events", "assessment_events", "attendance_events", "content_events"):
        col = raw[name]
        first = await col.find_one(sort=[("event_time", 1)], projection={"event_time": 1})
        last = await col.find_one(sort=[("event_time", -1)], projection={"event_time": 1})
        if first and first.get("event_time"):
            mins.append(_ensure_utc(first["event_time"]))
        if last and last.get("event_time"):
            maxs.append(_ensure_utc(last["event_time"]))
    if not mins or not maxs:
        return None
    return min(mins), max(maxs)


async def _run(args: argparse.Namespace) -> int:
    agg = WeeklyFeatureAggregator()

    if args.use_raw_range:
        bounds = await _raw_event_time_bounds()
        if bounds is None:
            print("No events found in raw collections; nothing to backfill.")
            return 1
        start, end = bounds
        print(f"Raw event span: {start.isoformat()} .. {end.isoformat()}")
    else:
        end = utc_now()
        start = end - timedelta(weeks=args.since_weeks)
        print(f"Backfilling from last {args.since_weeks} weeks: {start.isoformat()} .. {end.isoformat()}")

    total_writes = await agg.persist_weekly_snapshots_for_range(start, end)
    print(f"Weekly snapshot upserts completed (sum of per-week row counts): {total_writes}")
    print("Next: python -m ml.training.train_dropout_model --since-weeks", args.since_weeks)
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill student_weekly_features from Mongo raw events.")
    p.add_argument(
        "--since-weeks",
        type=int,
        default=20,
        help="When not using --use-raw-range, how far back from now to aggregate (default 20).",
    )
    p.add_argument(
        "--use-raw-range",
        action="store_true",
        help="Use min/max event_time across raw collections (covers full simulation window).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
