#!/usr/bin/env python3
"""Generate a Markdown EDA report for MongoDB `elearning_raw` simulation collections.

Collections expected (from seeding):
  - activity_events
  - assessment_events
  - attendance_events
  - simulation_users

Run from the BE directory with venv activated:

  python -m ml.eda.generate_eda_report --out ./reports/elearning_raw_eda.md

Environment (same as the API):
  MONGO_URI, MONGODB_RAW_DB (default: elearning_raw)
"""

from __future__ import annotations

import argparse
import os
import sys
import certifi
from datetime import datetime, timezone
from pathlib import Path

# Load BE/.env before reading settings
_BE_ROOT = Path(__file__).resolve().parents[2]
if str(_BE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BE_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(_BE_ROOT / ".env")
except ImportError:
    pass

from pymongo import MongoClient


COLLECTIONS = (
    "activity_events",
    "assessment_events",
    "attendance_events",
    "simulation_users",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fmt_dt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _mongo_client() -> MongoClient:
    uri = (os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "").strip()
    if not uri or uri == "...":
        raise SystemExit(
            "Set MONGO_URI to a valid mongodb:// or mongodb+srv:// connection string "
            '(not the literal "...").'
        )
    return MongoClient(uri, serverSelectionTimeoutMS=15_000, tlsCAFile=certifi.where())


def _coll_stats(db, name: str) -> dict:
    try:
        return dict(db.command("collStats", name))
    except Exception as e:
        return {"error": str(e)}


def _aggregate_list(coll, pipeline: list, *, limit: int | None = None) -> list:
    opts = {"allowDiskUse": True}
    cur = coll.aggregate(pipeline, **opts)
    out = list(cur)
    if limit is not None:
        return out[:limit]
    return out


def section_activity(db) -> list[str]:
    c = db["activity_events"]
    lines: list[str] = ["## activity_events", ""]
    n = c.estimated_document_count()
    lines.append(f"- **Estimated documents:** {n:,}")
    stats = _coll_stats(db, "activity_events")
    if "size" in stats:
        lines.append(f"- **Storage (collStats size):** {stats.get('size', 0) / (1024 * 1024):.2f} MiB")
    lines.append("")

    time_bounds = _aggregate_list(
        c,
        [
            {"$match": {"event_time": {"$exists": True}}},
            {"$group": {"_id": None, "min_t": {"$min": "$event_time"}, "max_t": {"$max": "$event_time"}}},
        ],
    )
    if time_bounds:
        tb = time_bounds[0]
        lines.append(f"- **event_time range:** {_fmt_dt(tb.get('min_t'))} → {_fmt_dt(tb.get('max_t'))}")
    lines.append("")

    lines.append("### event_type distribution")
    lines.append("")
    lines.append("| event_type | count |")
    lines.append("|---|---:|")
    for row in _aggregate_list(
        c,
        [
            {"$group": {"_id": "$event_type", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ],
    ):
        et = row["_id"] or "(null)"
        lines.append(f"| `{et}` | {row['n']:,} |")
    lines.append("")

    lines.append("### Top course_id (activity)")
    lines.append("")
    lines.append("| course_id | events |")
    lines.append("|---|---:|")
    for row in _aggregate_list(
        c,
        [
            {"$match": {"course_id": {"$exists": True, "$ne": None}}},
            {"$group": {"_id": "$course_id", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
            {"$limit": 20},
        ],
    ):
        lines.append(f"| {row['_id']} | {row['n']:,} |")
    lines.append("")

    user_count_rows = _aggregate_list(c, [{"$group": {"_id": "$user_id"}}, {"$count": "c"}])
    n_users = user_count_rows[0]["c"] if user_count_rows else 0
    lines.append(f"- **Distinct user_id:** {n_users:,}")
    lines.append("")

    lines.append("### Events per user (activity only) — deciles")
    lines.append("")
    max_users_for_deciles = 200_000
    if n_users == 0:
        lines.append("_No rows._")
    elif n_users > max_users_for_deciles:
        lines.append(
            f"_Skipped per-user deciles ({n_users:,} distinct users > {max_users_for_deciles:,})._ "
            "Use an aggregation notebook with sampling if you need this on huge data."
        )
    else:
        lines.append("*One pass per user (may take a minute on large collections).*")
        lines.append("")
        counts = [
            r["n"]
            for r in c.aggregate(
                [{"$group": {"_id": "$user_id", "n": {"$sum": 1}}}, {"$project": {"_id": 0, "n": 1}}],
                allowDiskUse=True,
            )
        ]
        counts.sort()
        m = len(counts)

        def decile(p: float) -> float:
            idx = min(max(int(p * (m - 1)), 0), m - 1)
            return float(counts[idx])

        lines.append("| percentile | events / user (activity) |")
        lines.append("|---|---:|")
        for label, p in [("min", 0.0), ("p10", 0.1), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("p90", 0.9), ("max", 1.0)]:
            lines.append(f"| {label} | {decile(p):.1f} |")
    lines.append("")
    return lines


def section_assessment(db) -> list[str]:
    c = db["assessment_events"]
    lines = ["## assessment_events", ""]
    lines.append(f"- **Estimated documents:** {c.estimated_document_count():,}")
    stats = _coll_stats(db, "assessment_events")
    if "size" in stats:
        lines.append(f"- **Storage (collStats size):** {stats.get('size', 0) / (1024 * 1024):.2f} MiB")
    lines.append("")

    time_bounds = _aggregate_list(
        c,
        [
            {"$match": {"event_time": {"$exists": True}}},
            {"$group": {"_id": None, "min_t": {"$min": "$event_time"}, "max_t": {"$max": "$event_time"}}},
        ],
    )
    if time_bounds:
        tb = time_bounds[0]
        lines.append(f"- **event_time range:** {_fmt_dt(tb.get('min_t'))} → {_fmt_dt(tb.get('max_t'))}")
    lines.append("")

    lines.append("### event_type distribution")
    lines.append("")
    lines.append("| event_type | count |")
    lines.append("|---|---:|")
    for row in _aggregate_list(
        c,
        [{"$group": {"_id": "$event_type", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}],
    ):
        lines.append(f"| `{row['_id']}` | {row['n']:,} |")
    lines.append("")

    lines.append("### timing_label (submission-related rows)")
    lines.append("")
    lines.append("| timing_label | count |")
    lines.append("|---|---:|")
    for row in _aggregate_list(
        c,
        [
            {"$match": {"timing_label": {"$exists": True, "$ne": None}}},
            {"$group": {"_id": "$timing_label", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ],
    ):
        lines.append(f"| `{row['_id']}` | {row['n']:,} |")
    lines.append("")

    score_stats = _aggregate_list(
        c,
        [
            {"$match": {"final_score": {"$exists": True, "$ne": None}}},
            {"$group": {"_id": None, "avg": {"$avg": "$final_score"}, "min": {"$min": "$final_score"}, "max": {"$max": "$final_score"}, "n": {"$sum": 1}}},
        ],
    )
    if score_stats:
        s = score_stats[0]
        lines.append("### final_score (non-null)")
        lines.append("")
        lines.append(f"- **count:** {s.get('n', 0):,}")
        lines.append(f"- **min / max / mean:** {s.get('min')} / {s.get('max')} / {round(s.get('avg', 0), 4)}")
        lines.append("")

    users = _aggregate_list(c, [{"$group": {"_id": "$user_id"}}, {"$count": "c"}])
    if users:
        lines.append(f"- **Distinct user_id:** {users[0].get('c', 0):,}")
    lines.append("")
    return lines


def section_attendance(db) -> list[str]:
    c = db["attendance_events"]
    lines = ["## attendance_events", ""]
    lines.append(f"- **Estimated documents:** {c.estimated_document_count():,}")
    stats = _coll_stats(db, "attendance_events")
    if "size" in stats:
        lines.append(f"- **Storage (collStats size):** {stats.get('size', 0) / (1024 * 1024):.2f} MiB")
    lines.append("")

    time_bounds = _aggregate_list(
        c,
        [
            {"$match": {"event_time": {"$exists": True}}},
            {"$group": {"_id": None, "min_t": {"$min": "$event_time"}, "max_t": {"$max": "$event_time"}}},
        ],
    )
    if time_bounds:
        tb = time_bounds[0]
        lines.append(f"- **event_time range:** {_fmt_dt(tb.get('min_t'))} → {_fmt_dt(tb.get('max_t'))}")
    lines.append("")

    lines.append("### status distribution")
    lines.append("")
    lines.append("| status | count |")
    lines.append("|---|---:|")
    for row in _aggregate_list(
        c,
        [{"$group": {"_id": "$status", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}],
    ):
        lines.append(f"| `{row['_id']}` | {row['n']:,} |")
    lines.append("")

    lines.append("### event_type distribution")
    lines.append("")
    lines.append("| event_type | count |")
    lines.append("|---|---:|")
    for row in _aggregate_list(
        c,
        [{"$group": {"_id": "$event_type", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}],
    ):
        lines.append(f"| `{row['_id']}` | {row['n']:,} |")
    lines.append("")
    return lines


def section_simulation_users(db) -> list[str]:
    c = db["simulation_users"]
    lines = ["## simulation_users", ""]
    lines.append(f"- **Estimated documents:** {c.estimated_document_count():,}")
    stats = _coll_stats(db, "simulation_users")
    if "size" in stats:
        lines.append(f"- **Storage (collStats size):** {stats.get('size', 0) / (1024 * 1024):.2f} MiB")
    lines.append("")

    lines.append("### role_id")
    lines.append("")
    lines.append("| role_id | count |")
    lines.append("|---|---:|")
    for row in _aggregate_list(
        c,
        [{"$group": {"_id": "$role_id", "n": {"$sum": 1}}}, {"$sort": {"_id": 1}}],
    ):
        lines.append(f"| {row['_id']} | {row['n']:,} |")
    lines.append("")

    lines.append("### persona")
    lines.append("")
    lines.append("| persona | count |")
    lines.append("|---|---:|")
    for row in _aggregate_list(
        c,
        [{"$group": {"_id": "$persona", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}],
    ):
        lines.append(f"| `{row['_id']}` | {row['n']:,} |")
    lines.append("")

    lines.append("### department_id (top 20)")
    lines.append("")
    lines.append("| department_id | count |")
    lines.append("|---|---:|")
    for row in _aggregate_list(
        c,
        [{"$group": {"_id": "$department_id", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 20}],
    ):
        lines.append(f"| {row['_id']} | {row['n']:,} |")
    lines.append("")

    gpa = _aggregate_list(
        c,
        [
            {"$match": {"current_gpa": {"$exists": True, "$ne": None}}},
            {"$group": {"_id": None, "avg": {"$avg": "$current_gpa"}, "min": {"$min": "$current_gpa"}, "max": {"$max": "$current_gpa"}, "n": {"$sum": 1}}},
        ],
    )
    if gpa:
        g = gpa[0]
        lines.append("### current_gpa (students only, if present)")
        lines.append("")
        lines.append(f"- **count:** {g.get('n', 0):,}")
        lines.append(f"- **min / max / mean:** {g.get('min')} / {g.get('max')} / {round(g.get('avg', 0), 4)}")
        lines.append("")
    return lines


def section_quality(db) -> list[str]:
    lines = ["## Data quality checks", ""]
    for name in COLLECTIONS:
        c = db[name]
        missing_user = c.count_documents({"user_id": {"$in": [None, ""]}})
        lines.append(f"- **{name}** — documents with missing `user_id`: {missing_user:,}")
    lines.append("")

    lines.append("### idempotency_key cardinality (events)")
    lines.append("")
    for name in ("activity_events", "assessment_events", "attendance_events"):
        c = db[name]
        total = c.estimated_document_count()
        nk = _aggregate_list(c, [{"$group": {"_id": "$idempotency_key"}}, {"$count": "c"}])
        distinct = nk[0]["c"] if nk else 0
        lines.append(f"- **{name}:** documents ≈ {total:,}, distinct `idempotency_key` ≈ {distinct:,}")
    lines.append("")
    return lines


def section_cross_cut(db) -> list[str]:
    lines = ["## Cross-collection summary", ""]
    sim = db["simulation_users"]
    students = _aggregate_list(sim, [{"$match": {"role_id": 3}}, {"$count": "c"}])
    lecturers = _aggregate_list(sim, [{"$match": {"role_id": 2}}, {"$count": "c"}])
    if students:
        lines.append(f"- **Simulation students (role_id=3):** {students[0].get('c', 0):,}")
    if lecturers:
        lines.append(f"- **Simulation lecturers (role_id=2):** {lecturers[0].get('c', 0):,}")
    lines.append("")
    lines.append("- Join raw events to labels: `simulation_users.user_id` ↔ `*.user_id`; use `persona` as a **simulation ground-truth behavior segment** (not a production label).")
    lines.append("")
    return lines


def generate_report(db_name: str) -> str:
    client = _mongo_client()
    db = client[db_name]

    parts: list[str] = [
        "# EDA report: MongoDB raw simulation layer",
        "",
        f"- **Database:** `{db_name}`",
        f"- **Generated (UTC):** {_utc_now_iso()}",
        "- **Collections:** `activity_events`, `assessment_events`, `attendance_events`, `simulation_users`",
        "",
        "---",
        "",
    ]

    for fn in (section_cross_cut, section_quality, section_activity, section_assessment, section_attendance, section_simulation_users):
        parts.extend(fn(db))
        parts.append("---")
        parts.append("")

    parts.append("## Interpretation notes")
    parts.append("")
    parts.append("- **Time axis:** Use `event_time` for behavioral sequencing; `created_at` may mirror it when data was clock-warped at ingest.")
    parts.append("- **Personas** (`star`, `steady`, …) are useful for **sanity checks** and stratified evaluation; replace with real outcomes before production decisions.")
    parts.append("- **assessment_events** mixes `submission_created` and `graded`; aggregate features should count submissions once (see weekly feature job).")
    parts.append("")

    client.close()
    return "\n".join(parts).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="EDA Markdown report for elearning_raw.")
    parser.add_argument(
        "--db",
        default=os.getenv("MONGODB_RAW_DB", "elearning_raw"),
        help="Mongo database name (default: MONGODB_RAW_DB or elearning_raw)",
    )
    parser.add_argument("--out", type=Path, default=None, help="Write report to this file (UTF-8).")
    args = parser.parse_args()

    report = generate_report(args.db)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"Wrote {args.out.resolve()}")
    else:
        print(report)


if __name__ == "__main__":
    main()
