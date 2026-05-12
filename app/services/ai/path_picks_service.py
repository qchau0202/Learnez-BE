"""Persistence layer for a student's personalised learning-path picks.

The Learning Path tab on the analytics dashboard renders three groups of
courses:

1. **Completed** — courses already enrolled in with ``is_complete=true`` and
   a final grade. These are immutable from the student's side (see the
   removal guard below).
2. **In progress** — currently enrolled, still active.
3. **Upcoming / personal picks** — courses the AI has suggested *plus*
   any extra ones the student has actively pinned to their path via the
   "Add to my path" button. These are the rows owned by this module.

The picks live in ``learnez_ai.student_path_picks``. Each document is one
``(user_id, course_id)`` decision with provenance and a soft-delete marker
so we keep the analytics history (e.g. "student removed course X after
seeing risk score").

Removal guard
-------------

Per product spec: a student must **not** be able to remove a course that
already ended and contributed to their GPA. The guard:

* If the course is already in ``public.course_enrollments`` for the
  student, **and** ``courses.is_complete=true``, **and** the student has
  at least one ``assignment_submissions.final_score`` recorded on it →
  raise :class:`PathPickRemovalForbidden`.
* If the course is only a "personal pick" (no real enrollment yet) or is
  an upcoming/in-progress course without finalised grades → free to
  remove.

The rule is enforced server-side; the API surface adds a server-computed
``can_remove`` boolean to each card so the UI can render an
enabled / disabled trash icon without duplicating the logic on the
client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from app.core.database import get_mongo_ai_db, get_supabase

logger = logging.getLogger(__name__)

COLLECTION_NAME = "student_path_picks"


class PathPickError(Exception):
    """Base class for path-pick service errors."""


class PathPickNotFound(PathPickError):
    """The requested pick doesn't exist (or is already soft-removed)."""


class PathPickRemovalForbidden(PathPickError):
    """Removal blocked because the course is completed + has a GPA."""

    def __init__(self, course_id: int, reason: str = "completed_with_grade") -> None:
        super().__init__(
            f"Cannot remove course_id={course_id}: {reason}. "
            "Completed courses that contribute to your GPA are locked."
        )
        self.course_id = course_id
        self.reason = reason


class CourseNotInCatalog(PathPickError):
    """The requested course id is not present in ``public.courses``."""


@dataclass(frozen=True, slots=True)
class PathPick:
    """One active learning-path pick as exposed to the API layer."""

    user_id: str
    course_id: int
    course_code: str | None
    title: str | None
    source: str  # "ai" | "manual"
    added_at: datetime
    can_remove: bool = True
    locked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "course_id": self.course_id,
            "course_code": self.course_code,
            "title": self.title,
            "source": self.source,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "can_remove": self.can_remove,
            "locked_reason": self.locked_reason,
        }


@dataclass(frozen=True, slots=True)
class CourseSnapshot:
    """The bits of ``public.courses`` we care about for pick rendering."""

    id: int
    course_code: str | None
    title: str | None
    is_complete: bool
    has_grade: bool = False

    @property
    def can_remove(self) -> bool:
        # GPA-bearing completed courses are locked. Anything else is free.
        return not (self.is_complete and self.has_grade)

    @property
    def locked_reason(self) -> str | None:
        return "completed_with_grade" if not self.can_remove else None


# --------------------------------------------------------------------------- #
# Internal Supabase look-ups
# --------------------------------------------------------------------------- #


def _fetch_course_snapshots(course_ids: Iterable[int]) -> dict[int, CourseSnapshot]:
    """Hydrate ``CourseSnapshot`` rows for the given course ids in a single batch."""
    ids = sorted({int(c) for c in course_ids})
    if not ids:
        return {}
    sb = get_supabase(service_role=True)
    if sb is None:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY not configured")
    rows = (
        sb.table("courses")
        .select("id, course_code, title, is_complete")
        .in_("id", ids)
        .execute()
        .data
        or []
    )
    return {
        int(r["id"]): CourseSnapshot(
            id=int(r["id"]),
            course_code=r.get("course_code"),
            title=r.get("title"),
            is_complete=bool(r.get("is_complete")),
        )
        for r in rows
        if r.get("id") is not None
    }


def _course_has_grade(user_id: str, course_id: int) -> bool:
    """``True`` if at least one finalised submission ``final_score`` exists.

    A single graded assignment is enough to lock removal — the cumulative
    GPA already incorporates it and unilaterally dropping the course would
    break the audit trail.
    """
    sb = get_supabase(service_role=True)
    if sb is None:
        return False
    rows = (
        sb.table("assignment_submissions")
        .select(
            "final_score, assignment_id, "
            "assignments!inner(module_id, modules!inner(course_id))"
        )
        .eq("student_id", user_id)
        .eq("assignments.modules.course_id", course_id)
        .not_.is_("final_score", None)
        .limit(1)
        .execute()
        .data
        or []
    )
    return bool(rows)


def _annotate_with_grade(
    snapshot: CourseSnapshot, *, user_id: str
) -> CourseSnapshot:
    """Re-hydrate the snapshot with ``has_grade`` filled in.

    We only look up the grade flag for *completed* courses — for active /
    upcoming courses the answer is always ``False`` anyway and the extra
    Supabase round-trip would be wasted.
    """
    if not snapshot.is_complete:
        return snapshot
    has_grade = _course_has_grade(user_id, snapshot.id)
    return CourseSnapshot(
        id=snapshot.id,
        course_code=snapshot.course_code,
        title=snapshot.title,
        is_complete=snapshot.is_complete,
        has_grade=has_grade,
    )


# --------------------------------------------------------------------------- #
# Public service API
# --------------------------------------------------------------------------- #


async def list_active_picks(user_id: str) -> list[PathPick]:
    """Return every currently-active (not soft-removed) pick for ``user_id``.

    Each pick carries denormalised course code / title (fast UI render) and
    a server-computed ``can_remove`` flag so the client doesn't have to
    mirror the removal rule.
    """
    ai_db = get_mongo_ai_db()
    cursor = ai_db[COLLECTION_NAME].find(
        {"user_id": user_id, "removed_at": None},
        sort=[("added_at", -1)],
    )
    raw_docs = await cursor.to_list(length=None)
    if not raw_docs:
        return []

    course_ids = {int(d["course_id"]) for d in raw_docs if d.get("course_id") is not None}
    snapshots = _fetch_course_snapshots(course_ids)

    picks: list[PathPick] = []
    for doc in raw_docs:
        course_id = int(doc["course_id"])
        snapshot = snapshots.get(course_id)
        if snapshot is None:
            # The catalog dropped the course (admin deleted it). Keep the
            # pick visible but unremovable so the user notices.
            picks.append(
                PathPick(
                    user_id=user_id,
                    course_id=course_id,
                    course_code=doc.get("course_code"),
                    title=doc.get("title"),
                    source=doc.get("source", "manual"),
                    added_at=doc.get("added_at") or datetime.now(timezone.utc),
                    can_remove=False,
                    locked_reason="course_missing",
                )
            )
            continue
        snapshot = _annotate_with_grade(snapshot, user_id=user_id)
        picks.append(
            PathPick(
                user_id=user_id,
                course_id=course_id,
                course_code=snapshot.course_code or doc.get("course_code"),
                title=snapshot.title or doc.get("title"),
                source=doc.get("source", "manual"),
                added_at=doc.get("added_at") or datetime.now(timezone.utc),
                can_remove=snapshot.can_remove,
                locked_reason=snapshot.locked_reason,
            )
        )
    return picks


async def add_pick(
    *,
    user_id: str,
    course_id: int,
    source: str = "manual",
) -> PathPick:
    """Insert or revive a pick.

    If the (user, course) document already exists but is soft-removed, we
    flip ``removed_at`` back to ``None`` and refresh ``added_at`` — this
    keeps the unique index honest while letting students re-add a course
    they previously removed.
    """
    snapshots = _fetch_course_snapshots([course_id])
    snapshot = snapshots.get(int(course_id))
    if snapshot is None:
        raise CourseNotInCatalog(
            f"course_id={course_id} not found in public.courses"
        )

    ai_db = get_mongo_ai_db()
    now = datetime.now(timezone.utc)
    update = {
        "$set": {
            "user_id": user_id,
            "course_id": int(course_id),
            "course_code": snapshot.course_code,
            "title": snapshot.title,
            "source": source,
            "added_at": now,
            "removed_at": None,
            "removal_reason": None,
        }
    }
    await ai_db[COLLECTION_NAME].update_one(
        {"user_id": user_id, "course_id": int(course_id)},
        update,
        upsert=True,
    )

    snapshot = _annotate_with_grade(snapshot, user_id=user_id)
    return PathPick(
        user_id=user_id,
        course_id=snapshot.id,
        course_code=snapshot.course_code,
        title=snapshot.title,
        source=source,
        added_at=now,
        can_remove=snapshot.can_remove,
        locked_reason=snapshot.locked_reason,
    )


async def remove_pick(
    *,
    user_id: str,
    course_id: int,
    reason: str = "student_skipped",
) -> None:
    """Soft-remove a pick by setting ``removed_at``.

    Raises :class:`PathPickRemovalForbidden` if the course is locked
    (completed + has a final-score submission), and
    :class:`PathPickNotFound` if there is no active pick to remove.

    The course is still allowed to remain in ``course_enrollments`` —
    a path pick is purely a wishlist marker, removing it never
    unenrolls the student.
    """
    snapshots = _fetch_course_snapshots([course_id])
    snapshot = snapshots.get(int(course_id))
    if snapshot is not None:
        snapshot = _annotate_with_grade(snapshot, user_id=user_id)
        if not snapshot.can_remove:
            raise PathPickRemovalForbidden(course_id=course_id, reason=snapshot.locked_reason or "locked")

    ai_db = get_mongo_ai_db()
    result = await ai_db[COLLECTION_NAME].update_one(
        {"user_id": user_id, "course_id": int(course_id), "removed_at": None},
        {
            "$set": {
                "removed_at": datetime.now(timezone.utc),
                "removal_reason": reason,
            }
        },
    )
    if result.matched_count == 0:
        raise PathPickNotFound(
            f"No active pick for user_id={user_id} course_id={course_id}"
        )


async def picks_summary(user_id: str) -> dict[str, Any]:
    """Compact summary for the path overview header — counts + last-touched."""
    ai_db = get_mongo_ai_db()
    active = await ai_db[COLLECTION_NAME].count_documents(
        {"user_id": user_id, "removed_at": None}
    )
    total = await ai_db[COLLECTION_NAME].count_documents({"user_id": user_id})
    last_doc = await ai_db[COLLECTION_NAME].find_one(
        {"user_id": user_id},
        sort=[("added_at", -1)],
    )
    return {
        "active_picks": int(active),
        "lifetime_picks": int(total),
        "last_pick_at": (
            last_doc.get("added_at").isoformat()
            if last_doc and isinstance(last_doc.get("added_at"), datetime)
            else None
        ),
    }


__all__ = [
    "COLLECTION_NAME",
    "PathPick",
    "PathPickError",
    "PathPickNotFound",
    "PathPickRemovalForbidden",
    "CourseNotInCatalog",
    "add_pick",
    "remove_pick",
    "list_active_picks",
    "picks_summary",
]
