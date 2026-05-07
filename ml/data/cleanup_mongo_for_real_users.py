#!/usr/bin/env python3
"""Delete Mongo documents whose ``user_id`` is not a real Supabase user.

The simulation seed wrote events and weekly features for ~1,000 ``user_id``s
that don't exist in Supabase ``users`` and therefore have no faculty,
department, class, or enrollment. Those rows are noise; this script drops them.

Targets:

* ``elearning_raw.activity_events``
* ``elearning_raw.assessment_events``
* ``elearning_raw.attendance_events``
* ``elearning_raw.content_events``
* ``elearning_raw.simulation_users``  (only when ``--include-simulation-users``)
* ``learnez_ai.student_weekly_features``
* ``learnez_ai.student_daily_features``
* ``learnez_ai.course_engagement_features``
* ``learnez_ai.risk_scores``

Default mode is **dry-run**: nothing is deleted. Pass ``--apply`` to actually
delete. Pass ``--include-simulation-users`` to also drop the persona seed
collection (only safe if you no longer use ``--label-mode persona*`` for
training).

Usage (from BE/, venv on):

  # See what would be deleted
  python -m ml.data.cleanup_mongo_for_real_users

  # Actually delete
  python -m ml.data.cleanup_mongo_for_real_users --apply

  # Also drop simulation_users persona seed
  python -m ml.data.cleanup_mongo_for_real_users --apply --include-simulation-users
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from app.core.database import get_mongo_ai_db, get_mongo_raw_db, get_supabase


RAW_COLLECTIONS = (
    "activity_events",
    "assessment_events",
    "attendance_events",
    "content_events",
)

AI_COLLECTIONS = (
    "student_weekly_features",
    "student_daily_features",
    "course_engagement_features",
    "risk_scores",
)


def _real_user_ids() -> set[str]:
    sb = get_supabase(service_role=True)
    if not sb:
        raise RuntimeError("Missing SUPABASE_SERVICE_ROLE_KEY; cannot identify real users.")
    rows = sb.table("users").select("user_id").execute().data or []
    return {str(r.get("user_id")) for r in rows if r.get("user_id")}


async def _delete_orphans(
    db,
    collection_name: str,
    real_user_ids: set[str],
    *,
    apply: bool,
) -> dict[str, Any]:
    coll = db[collection_name]
    total_before = await coll.count_documents({})
    if total_before == 0:
        return {"collection": collection_name, "before": 0, "to_delete": 0, "deleted": 0}

    # Stream distinct user_ids and split into real vs orphan.
    cursor = coll.find({}, {"user_id": 1, "_id": 0})
    cursor.batch_size(1000)
    orphan_ids: set[str] = set()
    real_in_collection: set[str] = set()
    no_uid = 0
    async for d in cursor:
        uid = d.get("user_id")
        if uid is None:
            no_uid += 1
            continue
        s = str(uid)
        if s in real_user_ids:
            real_in_collection.add(s)
        else:
            orphan_ids.add(s)

    if not orphan_ids:
        return {
            "collection": collection_name,
            "before": total_before,
            "to_delete": 0,
            "deleted": 0,
            "distinct_real": len(real_in_collection),
            "no_user_id": no_uid,
        }

    # Count and (optionally) delete in one $in pass per chunk to keep the
    # query small. Atlas has a 16 MB BSON limit; 5k UUIDs per chunk is safe.
    orphan_list = sorted(orphan_ids)
    to_delete = 0
    deleted = 0
    chunk_size = 5000
    for i in range(0, len(orphan_list), chunk_size):
        chunk = orphan_list[i : i + chunk_size]
        if apply:
            res = await coll.delete_many({"user_id": {"$in": chunk}})
            deleted += int(res.deleted_count or 0)
        else:
            to_delete += await coll.count_documents({"user_id": {"$in": chunk}})

    return {
        "collection": collection_name,
        "before": total_before,
        "to_delete": to_delete if not apply else deleted,
        "deleted": deleted,
        "distinct_orphan_user_ids": len(orphan_ids),
        "distinct_real_user_ids": len(real_in_collection),
        "no_user_id": no_uid,
    }


async def _amain(args: argparse.Namespace) -> int:
    real_ids = _real_user_ids()
    print(f"real_user_ids_in_supabase: {len(real_ids)}")
    if not real_ids:
        print("Aborting: Supabase users table is empty. Refusing to wipe Mongo against an empty allow-list.")
        return 1

    raw_db = get_mongo_raw_db()
    ai_db = get_mongo_ai_db()
    print(f"raw_db: {raw_db.name}")
    print(f"ai_db:  {ai_db.name}")
    print(f"mode:   {'APPLY (will delete)' if args.apply else 'DRY-RUN (no changes)'}")
    print()

    print("[RAW LAYER]")
    for name in RAW_COLLECTIONS:
        result = await _delete_orphans(raw_db, name, real_ids, apply=args.apply)
        verb = "deleted" if args.apply else "would_delete"
        print(
            f"- {name}: before={result['before']:>7} {verb}={result['to_delete']:>7} "
            f"orphan_uids={result.get('distinct_orphan_user_ids', 0)} "
            f"real_uids={result.get('distinct_real_user_ids', 0)} "
            f"no_uid={result.get('no_user_id', 0)}"
        )
    if args.include_simulation_users:
        sim_total = await raw_db["simulation_users"].count_documents({})
        if args.apply:
            res = await raw_db["simulation_users"].delete_many({})
            print(f"- simulation_users: dropped {int(res.deleted_count or 0)} of {sim_total}")
        else:
            print(f"- simulation_users: would drop all {sim_total} rows")
    print()

    print("[AI LAYER]")
    for name in AI_COLLECTIONS:
        result = await _delete_orphans(ai_db, name, real_ids, apply=args.apply)
        verb = "deleted" if args.apply else "would_delete"
        print(
            f"- {name}: before={result['before']:>7} {verb}={result['to_delete']:>7} "
            f"orphan_uids={result.get('distinct_orphan_user_ids', 0)} "
            f"real_uids={result.get('distinct_real_user_ids', 0)} "
            f"no_uid={result.get('no_user_id', 0)}"
        )

    print()
    if args.apply:
        print("DONE. Re-run `python -m ml.training.sample_dropout_predictions` to refresh risk_scores.")
    else:
        print("DRY-RUN complete. Re-run with `--apply` to actually delete.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Delete Mongo documents whose user_id is not a real Supabase user."
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete. Without this flag the script just reports counts.",
    )
    p.add_argument(
        "--include-simulation-users",
        action="store_true",
        help="Also drop the persona seed collection (elearning_raw.simulation_users). "
        "Only safe if you no longer train on persona labels.",
    )
    return p.parse_args()


def main() -> int:
    return asyncio.run(_amain(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
