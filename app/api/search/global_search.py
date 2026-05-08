"""Global, role-scoped categorized search.

Used by the header search bar so users can quickly find the data they have
access to (courses, assignments, materials, notifications, people). The
endpoint runs short-lived `ilike` queries against Supabase tables and
combines the results into a single response that the FE can render as a
categorized dropdown.

Design notes
------------
- Keep per-category query budgets small (default 5, max 10) so the endpoint
  stays under ~150ms even for an admin with the full corpus available.
- All results are scoped by RBAC: students only ever see data from courses
  they are enrolled in; lecturers only see their own courses; admins see
  everything.
- The endpoint never raises on a partial failure — if one category lookup
  fails (e.g. transient PostgREST 502), we log and degrade gracefully so
  the UI still renders the remaining categories.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, require_roles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["Search"])

CategoryKey = Literal[
    "courses",
    "assignments",
    "materials",
    "notifications",
    "people",
]

DEFAULT_PER_CATEGORY = 5
MAX_PER_CATEGORY = 10


class SearchItem(BaseModel):
    """One match returned by the search endpoint.

    `url` is a frontend route the UI can navigate to when the result is
    selected. `score` is a coarse 0–1 ranking (substring match strength).
    """

    id: str
    title: str
    subtitle: Optional[str] = None
    description: Optional[str] = None
    url: str
    type: CategoryKey
    score: float = Field(default=0.5, ge=0.0, le=1.0)


class SearchCategory(BaseModel):
    key: CategoryKey
    label: str
    items: List[SearchItem]


class SearchResponse(BaseModel):
    query: str
    total: int
    categories: List[SearchCategory]


# --- helpers -----------------------------------------------------------------


def _sb():
    return get_supabase(service_role=True)


def _escape_or(token: str) -> str:
    """PostgREST `or_` filters reject `,` and `(` — escape conservatively."""
    return token.replace(",", " ").replace("(", " ").replace(")", " ").strip()


def _score_for(text: str | None, q: str) -> float:
    """Cheap substring-based relevance score (0..1).

    A title match scores highest; we further reward prefix matches because
    they tend to be what users typed most intentionally.
    """
    if not text or not q:
        return 0.4
    haystack = text.lower()
    needle = q.lower()
    if haystack == needle:
        return 1.0
    if haystack.startswith(needle):
        return 0.85
    if needle in haystack:
        return 0.65
    return 0.4


def _take(items: Iterable[SearchItem], n: int) -> List[SearchItem]:
    sorted_items = sorted(items, key=lambda x: x.score, reverse=True)
    return list(sorted_items)[:n]


def _student_course_ids(sb, uid: str) -> list[int]:
    enr = sb.table("course_enrollments").select("course_id").eq("student_id", uid).execute()
    return [r["course_id"] for r in (enr.data or []) if r.get("course_id") is not None]


def _lecturer_course_ids(sb, uid: str) -> list[int]:
    crs = sb.table("courses").select("id").eq("lecturer_id", uid).execute()
    return [r["id"] for r in (crs.data or []) if r.get("id") is not None]


def _module_ids_for_courses(sb, course_ids: list[int]) -> list[int]:
    if not course_ids:
        return []
    mods = sb.table("modules").select("id").in_("course_id", course_ids).execute()
    return [r["id"] for r in (mods.data or []) if r.get("id") is not None]


# --- per-category collectors -------------------------------------------------


def _search_courses(sb, q: str, role: str, uid: str, limit: int) -> list[SearchItem]:
    token = _escape_or(q)
    if not token:
        return []
    course_ids: list[int] | None
    if role == "Admin":
        course_ids = None
    elif role == "Lecturer":
        course_ids = _lecturer_course_ids(sb, uid)
        if not course_ids:
            return []
    else:
        course_ids = _student_course_ids(sb, uid)
        if not course_ids:
            return []

    try:
        builder = sb.table("courses").select(
            "id,title,course_code,class_room,academic_year,semester,is_complete,lecturer_id"
        )
        if course_ids is not None:
            builder = builder.in_("id", course_ids)
        builder = builder.or_(
            f"title.ilike.%{token}%,course_code.ilike.%{token}%,class_room.ilike.%{token}%"
        )
        res = builder.limit(limit * 2).execute()
    except Exception as exc:
        logger.warning("search/courses query failed: %s", exc)
        return []

    items: list[SearchItem] = []
    for r in res.data or []:
        cid = r.get("id")
        if cid is None:
            continue
        title = r.get("title") or f"Course {cid}"
        code = (r.get("course_code") or "").strip()
        sub_parts = [p for p in [code, r.get("academic_year"), r.get("class_room")] if p]
        items.append(
            SearchItem(
                id=str(cid),
                title=title,
                subtitle=" · ".join(str(p) for p in sub_parts) or None,
                url=f"/courses/{cid}/overview",
                type="courses",
                score=max(_score_for(title, q), _score_for(code, q)),
            )
        )
    return _take(items, limit)


def _search_assignments(sb, q: str, role: str, uid: str, limit: int) -> list[SearchItem]:
    token = _escape_or(q)
    if not token:
        return []
    if role == "Admin":
        module_ids: list[int] | None = None
        course_ids: list[int] = []
    elif role == "Lecturer":
        course_ids = _lecturer_course_ids(sb, uid)
        if not course_ids:
            return []
        module_ids = _module_ids_for_courses(sb, course_ids)
        if not module_ids:
            return []
    else:
        course_ids = _student_course_ids(sb, uid)
        if not course_ids:
            return []
        module_ids = _module_ids_for_courses(sb, course_ids)
        if not module_ids:
            return []

    try:
        builder = sb.table("assignments").select("id,title,description,module_id,due_date")
        if module_ids is not None:
            builder = builder.in_("module_id", module_ids)
        builder = builder.or_(f"title.ilike.%{token}%,description.ilike.%{token}%")
        res = builder.limit(limit * 2).execute()
    except Exception as exc:
        logger.warning("search/assignments query failed: %s", exc)
        return []

    rows = res.data or []
    if not rows:
        return []

    # Resolve course_id per assignment for nice subtitles + correct deep links.
    mods_needed = {r.get("module_id") for r in rows if r.get("module_id") is not None}
    course_by_module: dict[int, int] = {}
    course_titles: dict[int, str] = {}
    if mods_needed:
        try:
            mres = sb.table("modules").select("id,course_id").in_("id", list(mods_needed)).execute()
            course_by_module = {
                r["id"]: r["course_id"]
                for r in (mres.data or [])
                if r.get("id") is not None and r.get("course_id") is not None
            }
            cids = sorted({c for c in course_by_module.values()})
            if cids:
                cres = sb.table("courses").select("id,title").in_("id", cids).execute()
                course_titles = {
                    r["id"]: r.get("title") or f"Course {r['id']}"
                    for r in (cres.data or [])
                    if r.get("id") is not None
                }
        except Exception as exc:
            logger.warning("search/assignments course resolution failed: %s", exc)

    items: list[SearchItem] = []
    for r in rows:
        aid = r.get("id")
        if aid is None:
            continue
        mid = r.get("module_id")
        cid = course_by_module.get(mid) if mid is not None else None
        title = r.get("title") or f"Assignment {aid}"
        sub = course_titles.get(cid) if cid is not None else None
        url = f"/courses/{cid}/assignments" if cid is not None else "/courses"
        items.append(
            SearchItem(
                id=str(aid),
                title=title,
                subtitle=sub,
                description=(r.get("description") or "").strip() or None,
                url=url,
                type="assignments",
                score=max(
                    _score_for(title, q),
                    _score_for(r.get("description"), q),
                ),
            )
        )
    return _take(items, limit)


def _search_materials(sb, q: str, role: str, uid: str, limit: int) -> list[SearchItem]:
    token = _escape_or(q)
    if not token:
        return []
    if role == "Admin":
        module_ids: list[int] | None = None
    elif role == "Lecturer":
        course_ids = _lecturer_course_ids(sb, uid)
        if not course_ids:
            return []
        module_ids = _module_ids_for_courses(sb, course_ids)
        if not module_ids:
            return []
    else:
        course_ids = _student_course_ids(sb, uid)
        if not course_ids:
            return []
        module_ids = _module_ids_for_courses(sb, course_ids)
        if not module_ids:
            return []

    try:
        builder = sb.table("module_materials").select(
            "id,name,description,module_id,material_type,mime_type"
        )
        if module_ids is not None:
            builder = builder.in_("module_id", module_ids)
        builder = builder.or_(f"name.ilike.%{token}%,description.ilike.%{token}%")
        res = builder.limit(limit * 2).execute()
    except Exception as exc:
        logger.warning("search/materials query failed: %s", exc)
        return []

    rows = res.data or []
    if not rows:
        return []

    mods_needed = {r.get("module_id") for r in rows if r.get("module_id") is not None}
    course_by_module: dict[int, int] = {}
    course_titles: dict[int, str] = {}
    if mods_needed:
        try:
            mres = sb.table("modules").select("id,course_id").in_("id", list(mods_needed)).execute()
            course_by_module = {
                r["id"]: r["course_id"]
                for r in (mres.data or [])
                if r.get("id") is not None and r.get("course_id") is not None
            }
            cids = sorted({c for c in course_by_module.values()})
            if cids:
                cres = sb.table("courses").select("id,title").in_("id", cids).execute()
                course_titles = {
                    r["id"]: r.get("title") or f"Course {r['id']}"
                    for r in (cres.data or [])
                    if r.get("id") is not None
                }
        except Exception as exc:
            logger.warning("search/materials course resolution failed: %s", exc)

    items: list[SearchItem] = []
    for r in rows:
        mat_id = r.get("id")
        if mat_id is None:
            continue
        mid = r.get("module_id")
        cid = course_by_module.get(mid) if mid is not None else None
        title = (r.get("name") or "").strip() or f"Material {mat_id}"
        sub_parts = []
        if cid is not None and course_titles.get(cid):
            sub_parts.append(course_titles[cid])
        if r.get("material_type"):
            sub_parts.append(str(r["material_type"]).upper())
        url = f"/courses/{cid}/materials" if cid is not None else "/courses"
        items.append(
            SearchItem(
                id=str(mat_id),
                title=title,
                subtitle=" · ".join(sub_parts) or None,
                description=(r.get("description") or "").strip() or None,
                url=url,
                type="materials",
                score=max(
                    _score_for(title, q),
                    _score_for(r.get("description"), q),
                ),
            )
        )
    return _take(items, limit)


def _search_notifications(sb, q: str, uid: str, limit: int) -> list[SearchItem]:
    token = _escape_or(q)
    if not token:
        return []
    try:
        res = (
            sb.table("notifications")
            .select("id,title,body,course_id,notification_type,scenario,created_at")
            .eq("recipient_id", uid)
            .or_(f"title.ilike.%{token}%,body.ilike.%{token}%")
            .order("created_at", desc=True)
            .limit(limit * 2)
            .execute()
        )
    except Exception as exc:
        logger.warning("search/notifications query failed: %s", exc)
        return []

    items: list[SearchItem] = []
    for r in res.data or []:
        nid = r.get("id")
        if nid is None:
            continue
        title = r.get("title") or "Notification"
        sub = r.get("notification_type") or r.get("scenario") or None
        items.append(
            SearchItem(
                id=str(nid),
                title=title,
                subtitle=sub,
                description=(r.get("body") or "").strip() or None,
                url="/notifications",
                type="notifications",
                score=max(
                    _score_for(title, q),
                    _score_for(r.get("body"), q),
                ),
            )
        )
    return _take(items, limit)


def _search_people(sb, q: str, role: str, uid: str, limit: int) -> list[SearchItem]:
    """People search is admin/lecturer only."""
    if role not in ("Admin", "Lecturer"):
        return []
    token = _escape_or(q)
    if not token:
        return []

    student_filter_ids: list[str] | None = None
    if role == "Lecturer":
        # Lecturers can only find people who are in their own courses.
        cids = _lecturer_course_ids(sb, uid)
        if not cids:
            return []
        try:
            enr = (
                sb.table("course_enrollments")
                .select("student_id")
                .in_("course_id", cids)
                .execute()
            )
            student_filter_ids = sorted({r["student_id"] for r in (enr.data or []) if r.get("student_id")})
        except Exception as exc:
            logger.warning("search/people enrollment lookup failed: %s", exc)
            return []

    try:
        builder = sb.table("users").select("user_id,full_name,email,role_id,is_active")
        builder = builder.or_(f"full_name.ilike.%{token}%,email.ilike.%{token}%")
        if role == "Lecturer":
            if not student_filter_ids:
                return []
            builder = builder.in_("user_id", student_filter_ids).eq("role_id", 3)
        res = builder.eq("is_active", True).limit(limit * 2).execute()
    except Exception as exc:
        logger.warning("search/people query failed: %s", exc)
        return []

    items: list[SearchItem] = []
    for r in res.data or []:
        person_id = r.get("user_id")
        if not person_id:
            continue
        title = r.get("full_name") or r.get("email") or person_id
        sub_parts: list[str] = []
        role_label = ROLE_MAP.get(r.get("role_id"))
        if role_label:
            sub_parts.append(role_label)
        if r.get("email"):
            sub_parts.append(str(r["email"]))
        # Only admins land on the user-management page; lecturers don't have
        # a dedicated student page, so we link them to their analytics view.
        url = "/admin/users" if role == "Admin" else "/analytics"
        items.append(
            SearchItem(
                id=str(person_id),
                title=str(title),
                subtitle=" · ".join(sub_parts) or None,
                url=url,
                type="people",
                score=max(_score_for(r.get("full_name"), q), _score_for(r.get("email"), q)),
            )
        )
    return _take(items, limit)


# --- endpoint ----------------------------------------------------------------


CATEGORY_LABELS: dict[CategoryKey, str] = {
    "courses": "Courses",
    "assignments": "Assignments",
    "materials": "Materials",
    "notifications": "Notifications",
    "people": "People",
}


@router.get(
    "/",
    response_model=SearchResponse,
    summary="Cross-domain search scoped by role (courses, assignments, materials, notifications, people).",
)
async def global_search(
    q: str = Query(..., min_length=1, max_length=120, description="Search keyword (case-insensitive)."),
    limit: int = Query(
        DEFAULT_PER_CATEGORY,
        ge=1,
        le=MAX_PER_CATEGORY,
        description="Maximum results per category (1-10).",
    ),
    user: dict[str, Any] = Depends(require_roles(["Admin", "Lecturer", "Student"])),
):
    sb = _sb()
    if sb is None:
        # Should never happen in production, but keeps the endpoint shape
        # stable when env vars are missing during local boot.
        return SearchResponse(query=q, total=0, categories=[])

    role = ROLE_MAP.get(user["role_id"]) or "Student"
    uid = user["user_id"]
    keyword = q.strip()
    if not keyword:
        return SearchResponse(query=q, total=0, categories=[])

    courses = _search_courses(sb, keyword, role, uid, limit)
    assignments = _search_assignments(sb, keyword, role, uid, limit)
    materials = _search_materials(sb, keyword, role, uid, limit)
    notifications = _search_notifications(sb, keyword, uid, limit)
    people = _search_people(sb, keyword, role, uid, limit)

    ordered: list[tuple[CategoryKey, list[SearchItem]]] = [
        ("courses", courses),
        ("assignments", assignments),
        ("materials", materials),
        ("notifications", notifications),
        ("people", people),
    ]

    categories: list[SearchCategory] = []
    total = 0
    for key, items in ordered:
        if not items:
            continue
        categories.append(
            SearchCategory(
                key=key,
                label=CATEGORY_LABELS[key],
                items=items,
            )
        )
        total += len(items)

    return SearchResponse(query=keyword, total=total, categories=categories)
