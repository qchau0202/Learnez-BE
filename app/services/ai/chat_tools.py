"""Agentic tool registry for the learning-path chatbot.

Scope, per the user requirement:

* The chatbot can perform **analytics-only** actions:
    - Reorder the student's learning path (UI-only).
    - Apply a catalog alternative for a slot.
    - Navigate to specific analytics tabs.
    - Explain why a course was flagged remedial / accelerated.
    - Build a starter path for a brand-new student from the
      curriculum graph.
* It is **explicitly forbidden** from touching course data — there are
  no tools for creating, deleting, or editing courses; no enrollment
  mutations; no grade overrides.

Each tool returns a JSON-serialisable dict that the agent runtime
forwards to the frontend. The frontend then either re-queries the
analytics, re-orders local state, or navigates — the *backend* never
mutates persistent records on behalf of the chatbot.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.core.database import get_supabase
from app.services.ai.llm import ToolDefinition

logger = logging.getLogger(__name__)


# Allow-list of analytics tabs the bot may navigate to. Keeping this
# tight ensures the chatbot can never deep-link into administrative
# routes (e.g. ``/admin/courses``) which would violate the scope rule.
_NAVIGATION_ALLOWLIST: dict[str, str] = {
    "/analytics?tab=overview": "Analytics — Overview",
    "/analytics?tab=behavior": "Analytics — Behavior",
    "/analytics?tab=learning-path": "Analytics — Learning Path",
    "/analytics?tab=dropout": "Analytics — Risk Analysis",
}


_GRAPH_PATH = Path(__file__).resolve().parents[3] / "data" / "curriculum_graph.json"


@lru_cache(maxsize=1)
def _curriculum_graph() -> dict[str, Any]:
    try:
        return json.loads(_GRAPH_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Curriculum graph missing at %s", _GRAPH_PATH)
        return {"majors": {}}
    except Exception as exc:  # pragma: no cover - corrupt JSON
        logger.warning("Failed to parse curriculum graph: %s", exc)
        return {"majors": {}}


# --------------------------------------------------------------------------- #
# Tool implementations — each takes ``(student_id, args)`` and returns a JSON
# dict the runtime will forward to the FE. Tools never raise; they return a
# ``status`` field instead so the model can correct itself on the next turn.
# --------------------------------------------------------------------------- #


def _ok(action: str, **payload: Any) -> dict[str, Any]:
    return {"status": "ok", "action": action, **payload}


def _error(action: str, message: str) -> dict[str, Any]:
    return {"status": "error", "action": action, "message": message}


async def reorder_path(student_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Move a single course to a new position in the rendered path.

    The wire schema uses **1-indexed slots** (1 = first, -1 = last) to
    match how LLMs and humans naturally talk about positions. We
    convert to the 0-indexed value the FE pathBus expects on the way
    out so the rest of the stack stays in pythonic-array territory.
    """
    code = str(args.get("course_code") or "").strip().upper()
    pos_raw = args.get("to_position")
    if not code:
        return _error("reorder_path", "Missing course_code.")
    try:
        slot_one_indexed = int(pos_raw) if pos_raw is not None else 1
    except (TypeError, ValueError):
        return _error("reorder_path", "to_position must be an integer.")

    if slot_one_indexed == -1:
        zero_indexed = -1
        human_slot = "the end"
    else:
        clamped = max(1, slot_one_indexed)
        zero_indexed = clamped - 1
        human_slot = f"position {clamped}"
    return _ok(
        "reorder_path",
        course_code=code,
        to_position=zero_indexed,
        message=(
            f"Moved {code} to {human_slot} on your path. "
            "(UI-only — your enrolments are untouched.)"
        ),
    )


async def apply_alternative(student_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Apply one of the catalog alternatives for a path slot."""
    code = str(args.get("course_code") or "").strip().upper()
    alt_code = str(args.get("alternative_code") or "").strip().upper()
    if not code or not alt_code:
        return _error(
            "apply_alternative",
            "Both course_code and alternative_code are required.",
        )
    return _ok(
        "apply_alternative",
        course_code=code,
        alternative_code=alt_code,
        message=(
            f"Swapped {code} for {alt_code} on your path. (UI-only — this "
            "doesn't change your actual enrolment.)"
        ),
    )


async def navigate(student_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Open a specific analytics tab."""
    target = str(args.get("path") or "").strip()
    if target not in _NAVIGATION_ALLOWLIST:
        return _error(
            "navigate",
            "Navigation target is outside the allowed analytics tabs.",
        )
    return _ok(
        "navigate",
        path=target,
        label=_NAVIGATION_ALLOWLIST[target],
        message=f"Opening {_NAVIGATION_ALLOWLIST[target]}.",
    )


async def explain_recommendation(
    student_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Explain *why* a given course is currently flagged remedial /
    accelerated, using the same signals the analytics engine uses.

    The chatbot is expected to call this when a student asks "why is
    course X flagged?" — we look up the latest grade for the course
    and return a short, factual explanation.
    """
    code = str(args.get("course_code") or "").strip().upper()
    if not code:
        return _error("explain_recommendation", "Missing course_code.")

    sb = get_supabase(service_role=True)
    if not sb:
        return _error(
            "explain_recommendation", "Backend lookup unavailable."
        )

    try:
        course_rows = (
            sb.table("courses")
            .select("id, course_code, title")
            .eq("course_code", code)
            .limit(1)
            .execute()
            .data
        ) or []
    except Exception as exc:
        logger.warning("explain_recommendation course lookup failed: %s", exc)
        return _error("explain_recommendation", "Course lookup failed.")
    if not course_rows:
        return _error("explain_recommendation", f"No course matches code {code}.")
    course = course_rows[0]

    # The actual avg-grade computation lives in the analytics module; we
    # delegate to it here so the explanation always lines up with the
    # path response the FE just rendered.
    try:
        from app.api.activity.analytics import (
            _course_avg_grade_10,
            _student_submission_meta,
        )

        submission_rows, assignment_meta = _student_submission_meta(
            student_id, course_ids={int(course["id"])}
        )
        avg = _course_avg_grade_10(
            course_id=int(course["id"]),
            submission_rows=submission_rows,
            assignment_meta=assignment_meta,
        )
    except Exception as exc:
        logger.warning("explain_recommendation grade lookup failed: %s", exc)
        return _error("explain_recommendation", "Could not compute grade summary.")

    if avg is None or avg <= 0:
        kind = "info"
        explanation = (
            f"{code} ({course['title']}) has no graded work yet. The path engine "
            "leaves it neutral until a baseline is established."
        )
    elif avg < 5.5:
        kind = "remedial"
        explanation = (
            f"{code} ({course['title']}) has an average grade of {avg:.1f}/10. "
            "That's below the 5.5 threshold the engine uses for remedial flags, "
            "so we surface follow-up review courses to help you recover before "
            "moving on."
        )
    elif avg >= 8.5:
        kind = "accelerated"
        explanation = (
            f"{code} ({course['title']}) has an average grade of {avg:.1f}/10. "
            "That clears the 8.5 accelerated bar, so the engine offers "
            "more advanced same-department picks you could prepare for next."
        )
    else:
        kind = "info"
        explanation = (
            f"{code} ({course['title']}) is at {avg:.1f}/10 — solid mid-band, "
            "no remedial or accelerated flag from the engine."
        )

    return _ok(
        "explain_recommendation",
        course_code=code,
        course_title=course["title"],
        kind=kind,
        avg_grade_10=round(float(avg or 0.0), 2),
        explanation=explanation,
        # Mirror ``explanation`` into ``message`` so the chat runtime can
        # fall back to a friendly reply when the LLM forgets to include
        # narration alongside its tool call.
        message=explanation,
    )


async def summarize_recent_grades(
    student_id: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Per-course grade snapshot the chatbot can use for coaching prompts.

    Args:
        course_code: Optional. When set, the result is filtered to that
            single course; otherwise we summarise every enrolled course
            with at least one graded submission.

    Result fields per course:
        ``count`` — number of graded submissions
        ``avg_grade_10`` — average rescaled to 0..10
        ``latest_grade_10`` — most recent submission's score
        ``last_3`` — list of up to 3 most recent grades (oldest → newest)
        ``trend`` — ``improving`` / ``declining`` / ``stable`` based on
            comparing the second half of the timeline against the first
        ``flag`` — ``remedial`` / ``accelerated`` / ``info`` (same
            thresholds the analytics engine uses)
    """
    sb = get_supabase(service_role=True)
    if not sb:
        return _error("summarize_recent_grades", "Backend lookup unavailable.")

    code_filter = str(args.get("course_code") or "").strip().upper() or None

    # Pull enrolled course rows for this student (same join the path
    # endpoint uses) so the response is constrained to *their* courses.
    try:
        enr = (
            sb.table("course_enrollments")
            .select("course_id")
            .eq("student_id", student_id)
            .execute()
            .data
        ) or []
    except Exception as exc:
        logger.warning("summarize_recent_grades enrolment lookup failed: %s", exc)
        return _error("summarize_recent_grades", "Couldn't read your enrolments.")

    course_ids = {int(r["course_id"]) for r in enr if r.get("course_id") is not None}
    if not course_ids:
        return _ok(
            "summarize_recent_grades",
            courses=[],
            message="No enrolled courses on file yet.",
        )

    try:
        course_rows = (
            sb.table("courses")
            .select("id, course_code, title")
            .in_("id", sorted(course_ids))
            .execute()
            .data
        ) or []
    except Exception as exc:
        logger.warning("summarize_recent_grades courses lookup failed: %s", exc)
        return _error("summarize_recent_grades", "Course lookup failed.")

    if code_filter:
        course_rows = [
            c for c in course_rows
            if str(c.get("course_code") or "").upper() == code_filter
        ]
        if not course_rows:
            return _error(
                "summarize_recent_grades",
                f"You're not enrolled in any course matching {code_filter}.",
            )

    # Reuse the analytics helpers so coaching numbers always agree with
    # what the path / behavior tabs show.
    try:
        from app.api.activity.analytics import _student_submission_meta
    except Exception as exc:
        logger.warning("summarize_recent_grades helper import failed: %s", exc)
        return _error("summarize_recent_grades", "Internal helper unavailable.")

    submission_rows, assignment_meta = _student_submission_meta(
        student_id, course_ids={int(c["id"]) for c in course_rows}
    )

    # Bucket submissions by course and sort by ``submitted_at`` (the
    # field the rest of the codebase uses for time ordering).
    per_course: dict[int, list[dict[str, Any]]] = {
        int(c["id"]): [] for c in course_rows
    }
    for sub in submission_rows:
        if not sub.get("is_corrected") or sub.get("final_score") is None:
            continue
        aid = sub.get("assignment_id")
        if aid is None:
            continue
        meta = assignment_meta.get(int(aid))
        if not meta:
            continue
        cid = int(meta.get("course_id") or 0)
        if cid not in per_course:
            continue
        try:
            final = float(sub.get("final_score") or 0.0)
            total = float(meta.get("total_score") or 0.0)
        except (TypeError, ValueError):
            continue
        if total <= 0:
            continue
        score10 = max(0.0, min(10.0, (final / total) * 10.0))
        per_course[cid].append(
            {
                "score10": round(score10, 2),
                "submitted_at": sub.get("submitted_at") or sub.get("created_at"),
            }
        )

    summaries: list[dict[str, Any]] = []
    for course in course_rows:
        cid = int(course["id"])
        points = sorted(
            per_course.get(cid, []),
            key=lambda r: str(r.get("submitted_at") or ""),
        )
        if not points:
            summaries.append(
                {
                    "course_code": course.get("course_code"),
                    "course_title": course.get("title"),
                    "count": 0,
                    "avg_grade_10": 0.0,
                    "latest_grade_10": None,
                    "last_3": [],
                    "trend": "no_data",
                    "flag": "info",
                }
            )
            continue
        scores = [p["score10"] for p in points]
        avg = round(sum(scores) / len(scores), 2)
        latest = scores[-1]
        last_3 = scores[-3:]
        # Trend = compare second half avg vs first half avg.
        mid = len(scores) // 2 or 1
        first_half = scores[:mid] or scores[:1]
        second_half = scores[mid:] or scores[-1:]
        first_avg = sum(first_half) / len(first_half)
        second_avg = sum(second_half) / len(second_half)
        delta = second_avg - first_avg
        if delta >= 0.5:
            trend = "improving"
        elif delta <= -0.5:
            trend = "declining"
        else:
            trend = "stable"
        if avg < 5.5:
            flag = "remedial"
        elif avg >= 8.5:
            flag = "accelerated"
        else:
            flag = "info"
        summaries.append(
            {
                "course_code": course.get("course_code"),
                "course_title": course.get("title"),
                "count": len(scores),
                "avg_grade_10": avg,
                "latest_grade_10": latest,
                "last_3": last_3,
                "trend": trend,
                "flag": flag,
            }
        )

    summaries.sort(
        key=lambda r: (
            -1 if r["flag"] == "remedial" else (1 if r["flag"] == "accelerated" else 0),
            -float(r["avg_grade_10"] or 0.0),
        )
    )

    if code_filter and summaries:
        target = summaries[0]
        msg = (
            f"{target['course_code']} ({target['course_title']}): "
            f"avg {target['avg_grade_10']}/10 across {target['count']} graded "
            f"items, trend {target['trend']}, flag {target['flag']}."
        )
    elif summaries:
        # When the caller asked for the full enrolment list (no code
        # filter), include a compact directory of every course they're
        # taking. This is the message the chat runtime falls back to
        # when the LLM didn't add its own narration, so making it
        # informative matters more than being terse.
        flagged = [s for s in summaries if s["flag"] == "remedial"]
        accelerated = [s for s in summaries if s["flag"] == "accelerated"]
        directory = ", ".join(
            f"{s['course_code']} ({s['course_title']})"
            for s in summaries[:6]
        )
        if flagged:
            names = ", ".join(s["course_code"] for s in flagged[:3])
            msg = (
                f"Watching {names} for remedial follow-up. "
                f"Your enrolments: {directory}."
            )
        elif accelerated:
            names = ", ".join(s["course_code"] for s in accelerated[:3])
            msg = (
                f"Strong performance in {names}. "
                f"Your enrolments: {directory}."
            )
        else:
            msg = f"Your enrolments: {directory}."
    else:
        msg = "No enrolled courses on file yet."

    return _ok("summarize_recent_grades", courses=summaries, message=msg)


async def build_initial_path(student_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Use the curriculum graph to propose a starter path for a new
    student. Returns a list of milestone bundles the FE can render
    inline in the chat or write into the path."""
    major = str(args.get("major") or "").strip()
    interests = args.get("interests") or []
    if isinstance(interests, str):
        interests = [interests]

    graph = _curriculum_graph()
    majors = graph.get("majors") or {}
    selected = None
    for name, payload in majors.items():
        aliases = [a.lower() for a in (payload.get("aliases") or [])]
        if name.lower() == major.lower() or major.lower() in aliases:
            selected = (name, payload)
            break
    if selected is None:
        return _error(
            "build_initial_path",
            f"No curriculum graph entry for major '{major}'. "
            f"Known majors: {', '.join(majors.keys()) or '(none)'}.",
        )

    name, payload = selected
    milestones = payload.get("milestones") or []

    # Resolve milestone codes against the live catalog so any newly
    # imported courses surface here automatically. Codes that don't
    # exist yet are still returned (with ``available=False``) so the
    # chatbot can flag them as "coming soon" rather than silently
    # dropping them.
    sb = get_supabase(service_role=True)
    catalog_lookup: dict[str, dict[str, Any]] = {}
    if sb:
        try:
            all_codes: set[str] = set()
            for m in milestones:
                for c in m.get("course_codes") or []:
                    all_codes.add(str(c).upper())
            if all_codes:
                rows = (
                    sb.table("courses")
                    .select("id, course_code, title, semester, academic_year")
                    .in_("course_code", sorted(all_codes))
                    .execute()
                    .data
                ) or []
                catalog_lookup = {
                    str(r.get("course_code") or "").upper(): r for r in rows
                }
        except Exception as exc:
            logger.warning("build_initial_path catalog lookup failed: %s", exc)

    resolved_milestones: list[dict[str, Any]] = []
    for m in milestones:
        codes = [str(c).upper() for c in (m.get("course_codes") or [])]
        items: list[dict[str, Any]] = []
        for c in codes:
            row = catalog_lookup.get(c)
            if row:
                items.append(
                    {
                        "course_code": c,
                        "course_id": row.get("id"),
                        "title": row.get("title"),
                        "available": True,
                    }
                )
            else:
                items.append({"course_code": c, "available": False})
        resolved_milestones.append(
            {
                "semester": m.get("semester"),
                "label": m.get("label"),
                "themes": m.get("themes") or [],
                "courses": items,
            }
        )

    return _ok(
        "build_initial_path",
        major=name,
        interests=interests,
        milestones=resolved_milestones,
        message=(
            f"Drafted a starter path for {name}. These are recommendations — "
            "you decide which to enrol in once registration opens."
        ),
    )


# --------------------------------------------------------------------------- #
# Registry — name → (definition, handler)
# --------------------------------------------------------------------------- #


@dataclass
class RegisteredTool:
    definition: ToolDefinition
    handler: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _build_registry() -> dict[str, RegisteredTool]:
    return {
        "reorder_path": RegisteredTool(
            definition=ToolDefinition(
                name="reorder_path",
                description=(
                    "Move a single course to a new position in the student's "
                    "rendered learning path. UI-only; never mutates enrolments."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "course_code": {
                            "type": "string",
                            "description": "Course code, e.g. 'IT404'.",
                        },
                        "to_position": {
                            "type": "integer",
                            "description": (
                                "1-indexed target slot (1 = first slot, "
                                "2 = second, ...). Use -1 to move to the "
                                "very end of the path."
                            ),
                        },
                    },
                    "required": ["course_code", "to_position"],
                },
            ),
            handler=reorder_path,
        ),
        "apply_alternative": RegisteredTool(
            definition=ToolDefinition(
                name="apply_alternative",
                description=(
                    "Swap a path slot with one of its catalog alternatives. "
                    "UI-only; the underlying enrolment record is not changed."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "course_code": {"type": "string"},
                        "alternative_code": {"type": "string"},
                    },
                    "required": ["course_code", "alternative_code"],
                },
            ),
            handler=apply_alternative,
        ),
        "navigate": RegisteredTool(
            definition=ToolDefinition(
                name="navigate",
                description=(
                    "Open one of the allowed analytics tabs. Restricted to: "
                    + ", ".join(_NAVIGATION_ALLOWLIST.keys())
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "enum": list(_NAVIGATION_ALLOWLIST.keys()),
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["path"],
                },
            ),
            handler=navigate,
        ),
        "explain_recommendation": RegisteredTool(
            definition=ToolDefinition(
                name="explain_recommendation",
                description=(
                    "Explain why a course is currently flagged remedial / "
                    "accelerated / info, using the student's average grade."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "course_code": {"type": "string"},
                    },
                    "required": ["course_code"],
                },
            ),
            handler=explain_recommendation,
        ),
        "summarize_recent_grades": RegisteredTool(
            definition=ToolDefinition(
                name="summarize_recent_grades",
                description=(
                    "Per-course recent-grade snapshot for the student. "
                    "Returns count, average (0-10), latest grade, last "
                    "three grades, trend (improving / declining / stable), "
                    "and the same remedial / accelerated flag the path "
                    "engine uses. Use to coach the student or to back up "
                    "your reasoning before suggesting a swap."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "course_code": {
                            "type": "string",
                            "description": (
                                "Optional course code, e.g. 'IT404'. When "
                                "omitted, returns one entry per enrolled "
                                "course."
                            ),
                        }
                    },
                },
            ),
            handler=summarize_recent_grades,
        ),
        "build_initial_path": RegisteredTool(
            definition=ToolDefinition(
                name="build_initial_path",
                description=(
                    "Generate a starter learning path for a new student "
                    "without academic history, using the predefined "
                    "curriculum graph for their major."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "major": {
                            "type": "string",
                            "description": (
                                "Major name or short alias (e.g. 'Software "
                                "Engineering' or 'SE')."
                            ),
                        },
                        "interests": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Optional free-form interests captured during "
                                "the onboarding conversation."
                            ),
                        },
                    },
                    "required": ["major"],
                },
            ),
            handler=build_initial_path,
        ),
    }


_REGISTRY = _build_registry()


def list_tool_definitions() -> list[ToolDefinition]:
    return [t.definition for t in _REGISTRY.values()]


async def dispatch_tool(
    name: str, *, student_id: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    tool = _REGISTRY.get(name)
    if tool is None:
        return _error(name or "unknown", f"Tool '{name}' is not allowed.")
    try:
        return await tool.handler(student_id, arguments)
    except Exception as exc:  # pragma: no cover - defensive net
        logger.exception("Tool '%s' crashed: %s", name, exc)
        return _error(name, "Tool execution failed.")
