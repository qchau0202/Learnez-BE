"""One-command curriculum seeder.

Crawls the TDTU portal then upserts the result into Supabase. Each
phase is also runnable on its own:

* ``python -m ml.data.curriculum.crawler`` — just re-pull from TDTU.
* ``python -m ml.data.curriculum.sync``    — push an existing JSON to Supabase.
* ``python -m ml.data.curriculum.seed``    — end-to-end on a fresh box.

Usage (from ``BE/`` with the venv active and ``.env`` populated)::

    python -m ml.data.curriculum.seed --user studentId --pass 'password'

Add ``--crawl-only`` or ``--sync-only`` to run a single phase.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .crawler import CrawlerOptions, crawl_all
from .sync import (
    DEFAULT_ADMIN_USER_ID,
    DEFAULT_LECTURER_PRIMARY,
    DEFAULT_LECTURER_SECONDARY,
    DEFAULT_PRIMARY_WEIGHT,
    sync,
)

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    # Crawl knobs
    p.add_argument("--user", default=os.getenv("TDTU_USER"),
                   help="Student ID for SSO (TDTU_USER env). Required unless --sync-only.")
    p.add_argument("--pass", dest="password", default=os.getenv("TDTU_PASS"),
                   help="SSO password (TDTU_PASS env). Required unless --sync-only.")
    p.add_argument("--out", type=Path, default=Path("ml/data/syllabi.json"))
    p.add_argument("--debug-html", type=Path, default=Path("debug.html"))
    p.add_argument("--keep-html-dir", type=Path, default=None)
    p.add_argument("--sleep", type=float, default=0.4)

    # Sync knobs
    p.add_argument("--admin-id", default=DEFAULT_ADMIN_USER_ID)
    p.add_argument("--lecturer-primary", default=DEFAULT_LECTURER_PRIMARY)
    p.add_argument("--lecturer-secondary", default=DEFAULT_LECTURER_SECONDARY)
    p.add_argument("--primary-weight", type=float, default=DEFAULT_PRIMARY_WEIGHT)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--overwrite-descriptions", action="store_true")
    p.add_argument("--overwrite-titles", action="store_true")
    p.add_argument("--refresh-schedule", action="store_true")
    p.add_argument("--skip-existing", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    # Pipeline knobs
    p.add_argument("--crawl-only", action="store_true")
    p.add_argument("--sync-only", action="store_true")

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.getenv("CURRICULUM_SEEDER_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.crawl_only and args.sync_only:
        raise SystemExit("--crawl-only and --sync-only are mutually exclusive.")

    if not args.sync_only:
        if not args.user or not args.password:
            raise SystemExit(
                "Crawl phase needs --user/--pass (or TDTU_USER / TDTU_PASS env). "
                "If you only want to push an existing syllabi.json, pass --sync-only."
            )
        opts = CrawlerOptions(
            user=args.user,
            password=args.password,
            output=args.out,
            debug_html=args.debug_html,
            keep_html_dir=args.keep_html_dir,
            sleep_between_courses=args.sleep,
        )
        records = crawl_all(opts)
        ok = sum(1 for r in records if r.description)
        print(f"[crawl] records={len(records)} with_description={ok}")
        if ok == 0:
            print("[crawl] no descriptions captured; aborting before sync.")
            return 1

    if args.crawl_only:
        return 0

    summary = sync(
        input_path=args.out,
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
    print(
        f"[sync] inserted={summary['inserted']} updated={summary['updated']} "
        f"schedule_backfilled={summary['schedule_backfilled']} "
        f"schedule_unknown={summary['schedule_unknown']} "
        f"skipped_no_desc={summary['skipped_no_description']} "
        f"skipped_existing={summary['skipped_existing']} "
        f"errors={len(summary['errors'])}"
    )
    return 0 if not summary["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
