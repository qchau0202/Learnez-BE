"""Upsert crawled syllabi into the Supabase ``courses`` table.

Reads the JSON written by :mod:`ml.data.curriculum.crawler` and reflects
it into ``public.courses``. Idempotent on ``course_code``: re-runs keep
row ids stable and never duplicate. Each row gets:

* ``created_by`` = admin UUID (auditable as "admin-imported").
* ``lecturer_id`` = deterministic round-robin pick between two lecturers
  (the primary gets ~60% of caseload by default).
* All schedule columns from :mod:`ml.data.curriculum.schedule`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from pathlib import Path
from typing import Any

from app.core.database import get_supabase
from .schedule import CourseSchedule, schedule_for_code

logger = logging.getLogger(__name__)

SCHEDULE_COLUMNS: tuple[str, ...] = (
    "academic_year",
    "semester",
    "class_room",
    "course_session",
    "course_session_date",
    "course_session_duration",
    "course_occurences",
    "course_start_date",
    "course_end_date",
    "from_department",
    "is_complete",
)


DEFAULT_ADMIN_USER_ID = "0ee42aa9-05fc-4d71-a11b-22115bdc5202"
DEFAULT_LECTURER_PRIMARY = "7b5bbf8f-86b6-4bac-96e5-8c85c83c603f"
DEFAULT_LECTURER_SECONDARY = "de112d7d-1a3f-4f01-b74a-e52ba7ce4c9f"
DEFAULT_PRIMARY_WEIGHT = 0.6


def _svc():
    svc = get_supabase(service_role=True)
    if not svc:
        raise RuntimeError(
            "Missing SUPABASE_SERVICE_ROLE_KEY — set it in BE/.env before running."
        )
    return svc


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(
            f"syllabus file not found: {path}. Run ml.data.curriculum.crawler first."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON array of records.")
    return payload


def _existing_code_map(sb) -> dict[str, dict[str, Any]]:
    """``course_code -> row`` for everything already in the table."""
    columns = (
        "id, course_code, title, description, lecturer_id, created_by, "
        + ", ".join(SCHEDULE_COLUMNS)
    )
    rows = sb.table("courses").select(columns).execute().data or []
    return {str(r["course_code"]): r for r in rows if r.get("course_code")}


# Numeric columns where a literal ``0`` is meaningless and should trigger a
# refresh from the catalog (we saw legacy rows stuck at 0 in production).
_NUMERIC_SCHEDULE_FIELDS_ZERO_MEANS_EMPTY: frozenset[str] = frozenset(
    {"course_occurences", "course_session_duration", "semester", "from_department"}
)


def _is_schedule_field_empty(existing: dict[str, Any], column: str) -> bool:
    """``True`` if ``existing[column]`` is missing / null / empty / zero."""
    if column not in existing:
        return True
    value = existing[column]
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, str) and not value.strip():
        return True
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == 0
        and column in _NUMERIC_SCHEDULE_FIELDS_ZERO_MEANS_EMPTY
    ):
        return True
    return False


def _pick_lecturer(
    rng: random.Random, *, primary: str, secondary: str, primary_weight: float,
) -> str:
    """Deterministic per-course lecturer assignment (seeded by code)."""
    return primary if rng.random() < primary_weight else secondary


def sync(
    *,
    input_path: Path,
    admin_user_id: str,
    lecturer_primary: str,
    lecturer_secondary: str,
    primary_weight: float,
    seed: int,
    overwrite_descriptions: bool,
    overwrite_titles: bool,
    refresh_schedule: bool,
    skip_existing: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Drive the upsert. Returns a summary dict for the CLI."""
    records = _load_records(input_path)
    sb = _svc()
    existing_by_code = _existing_code_map(sb)
    summary = {
        "input": str(input_path),
        "total": len(records),
        "inserted": 0,
        "updated": 0,
        "schedule_backfilled": 0,
        "schedule_unknown": 0,
        "skipped_no_description": 0,
        "skipped_existing": 0,
        "errors": [],
        "lecturer_split": {lecturer_primary: 0, lecturer_secondary: 0},
    }

    for rec in records:
        code = (rec.get("code") or "").strip()
        if not code:
            summary["errors"].append({"code": None, "error": "record missing 'code'"})
            continue

        title = (rec.get("title") or "").strip() or None
        description = (rec.get("description") or "").strip() or None

        if not description:
            logger.warning("skip code=%s — no description in crawl payload", code)
            summary["skipped_no_description"] += 1
            continue

        existing = existing_by_code.get(code)
        if existing and skip_existing and (existing.get("description") or "").strip():
            summary["skipped_existing"] += 1
            continue

        rng = random.Random(f"{seed}:{code}")
        lecturer_id = _pick_lecturer(
            rng, primary=lecturer_primary, secondary=lecturer_secondary,
            primary_weight=primary_weight,
        )
        summary["lecturer_split"][lecturer_id] += 1

        schedule: CourseSchedule | None = schedule_for_code(code)
        if schedule is None:
            summary["schedule_unknown"] += 1
            logger.warning(
                "code=%s is not in curriculum catalog — schedule fields skipped", code,
            )
        schedule_payload: dict[str, Any] = (
            schedule.as_supabase_payload() if schedule is not None else {}
        )

        payload: dict[str, Any] = {
            "course_code": code,
            "description": description,
            "lecturer_id": lecturer_id,
            "created_by": admin_user_id,
        }
        if title:
            payload["title"] = title

        if existing:
            # Patch only what needs to change so admin-edited fields are preserved.
            patch: dict[str, Any] = {}
            if overwrite_titles and title and existing.get("title") != title:
                patch["title"] = title
            if overwrite_descriptions or not (existing.get("description") or "").strip():
                patch["description"] = description
            if not existing.get("lecturer_id"):
                patch["lecturer_id"] = lecturer_id
            if not existing.get("created_by"):
                patch["created_by"] = admin_user_id

            schedule_touched = False
            for column, value in schedule_payload.items():
                if refresh_schedule or _is_schedule_field_empty(existing, column):
                    patch[column] = value
                    schedule_touched = True
            if schedule_touched:
                summary["schedule_backfilled"] += 1

            if not patch:
                continue
            if dry_run:
                logger.info("dry-run UPDATE id=%s code=%s patch=%s", existing["id"], code, patch)
                summary["updated"] += 1
                continue
            try:
                sb.table("courses").update(patch).eq("id", existing["id"]).execute()
                summary["updated"] += 1
            except Exception as exc:
                logger.error("update failed for code=%s: %s", code, exc)
                summary["errors"].append({"code": code, "error": str(exc)})
            continue

        # Insert path
        payload.update(schedule_payload)
        if not payload.get("title"):
            payload["title"] = code
        payload.setdefault("is_complete", False)

        if dry_run:
            logger.info("dry-run INSERT code=%s payload=%s", code, payload)
            summary["inserted"] += 1
            continue
        try:
            sb.table("courses").insert(payload).execute()
            summary["inserted"] += 1
        except Exception as exc:
            logger.error("insert failed for code=%s: %s", code, exc)
            summary["errors"].append({"code": code, "error": str(exc)})

    return summary


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Reflect the crawler's JSON output into the Supabase courses table. "
            "Run after ml.data.curriculum.crawler has finished."
        )
    )
    p.add_argument("--input", type=Path, default=Path("ml/data/syllabi.json"))
    p.add_argument("--admin-id", default=os.getenv("SYLLABUS_ADMIN_ID", DEFAULT_ADMIN_USER_ID))
    p.add_argument("--lecturer-primary",
                   default=os.getenv("SYLLABUS_LECTURER_PRIMARY", DEFAULT_LECTURER_PRIMARY))
    p.add_argument("--lecturer-secondary",
                   default=os.getenv("SYLLABUS_LECTURER_SECONDARY", DEFAULT_LECTURER_SECONDARY))
    p.add_argument("--primary-weight", type=float, default=DEFAULT_PRIMARY_WEIGHT)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--overwrite-descriptions", action="store_true")
    p.add_argument("--overwrite-titles", action="store_true")
    p.add_argument("--refresh-schedule", action="store_true",
                   help="Force every schedule column to be re-derived.")
    p.add_argument("--skip-existing", action="store_true",
                   help="Don't touch rows that already have a non-empty description.")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("SYLLABUS_SYNC_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    summary = sync(
        input_path=args.input,
        admin_user_id=args.admin_id,
        lecturer_primary=args.lecturer_primary,
        lecturer_secondary=args.lecturer_secondary,
        primary_weight=args.primary_weight,
        seed=args.seed,
        overwrite_descriptions=args.overwrite_descriptions,
        overwrite_titles=args.overwrite_titles,
        refresh_schedule=args.refresh_schedule,
        skip_existing=args.skip_existing,
        dry_run=args.dry_run,
    )

    print("=" * 60)
    print(f"total      : {summary['total']}")
    print(f"inserted   : {summary['inserted']}")
    print(f"updated    : {summary['updated']}")
    print(f"schedule backfilled : {summary['schedule_backfilled']}")
    print(f"schedule unknown    : {summary['schedule_unknown']}")
    print(f"skipped(no desc)    : {summary['skipped_no_description']}")
    print(f"skipped(existing)   : {summary['skipped_existing']}")
    print(
        f"lecturer split      : primary={summary['lecturer_split'][args.lecturer_primary]} "
        f"secondary={summary['lecturer_split'][args.lecturer_secondary]}"
    )
    if summary["errors"]:
        print(f"errors     : {len(summary['errors'])}")
        for err in summary["errors"][:10]:
            print(f"  - {err['code']}: {err['error']}")
    print("=" * 60)
    if args.dry_run:
        print("dry-run: no changes were written to Supabase.")
    return 0 if not summary["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
