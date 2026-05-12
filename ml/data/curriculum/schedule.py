"""Deterministic schedule metadata for every catalog course.

The TDTU crawl only returns title + description; the LMS UI also needs
academic_year, classroom, session slot, day, start/end dates, etc. This
module computes those columns from the catalog itself so re-runs stay
stable and so introductory courses land in older academic years.

Rules
-----
* Tier 1 (2023-2024, completed, 10 sessions): year-one math + intro programming.
* Tier 2 (2024-2025, completed, 10 sessions): year-two intros / core systems.
* Tier 3 (2025-2026, in progress, 15 sessions): everything else.
* Semester alternates by catalog index. Sem 1 anchors on Sep 1; Sem 2 on
  Feb 16 (the standard TDTU post-Tết restart).
* Classroom: ``A101..A105`` then ``B101..B105`` etc. by catalog index.
* Day-of-week + time slot rotate through Mon–Fri × four 150-minute slots.
* ``from_department``: 2 (CS) for math/algorithms/ML codes, else 1 (SE).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

SESSION_SLOTS: tuple[str, ...] = (
    "7:00 - 9:30",
    "9:30 - 12:00",
    "12:45 - 15:15",
    "15:15 - 17:45",
)
SESSION_DURATION_MIN = 150

DAYS_OF_WEEK: tuple[str, ...] = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
_WEEKDAY_INDEX = {name: idx for idx, name in enumerate(DAYS_OF_WEEK)}

ROOMS_PER_LETTER = 5
ROOM_FIRST_NUMBER = 101

DEPT_SE = 1
DEPT_CS = 2

CS_DEPARTMENT_CODES: frozenset[str] = frozenset(
    {
        "501031", "501032", "501044", "502061", "502067",
        "503040", "503043", "503044", "503080", "503117",
        "504008", "504048", "504105", "505011", "505043", "505060",
    }
)

# Tier 1 — year-one foundational courses.
TIER1_CODES: frozenset[str] = frozenset(
    {"501031", "501032", "501042", "501044", "503005", "504008"}
)
# Tier 2 — year-two "Introduction to …" and core systems.
TIER2_CODES: frozenset[str] = frozenset(
    {
        "502044", "502045", "502046", "502047", "502048", "502049",
        "502051", "502061", "503040", "503043", "503044", "503080",
        "503116", "504088", "505060",
    }
)

# (tier_key, academic_year_label, sem1_year, sem2_year, is_complete_default, occurences)
TIER_CONFIG: tuple[tuple[str, str, int, int, bool, int], ...] = (
    ("tier1", "2023-2024", 2023, 2024, True, 10),
    ("tier2", "2024-2025", 2024, 2025, True, 10),
    ("tier3", "2025-2026", 2025, 2026, False, 15),
)
TIER_BY_KEY = {cfg[0]: cfg for cfg in TIER_CONFIG}

SEMESTER_1_MONTH_DAY: tuple[int, int] = (9, 1)
SEMESTER_2_MONTH_DAY: tuple[int, int] = (2, 16)


@dataclass(frozen=True, slots=True)
class CourseSchedule:
    """All schedule columns written into ``public.courses``."""

    academic_year: str
    semester: int
    class_room: str
    course_session: str
    course_session_date: str
    course_session_duration: int
    course_occurences: int
    course_start_date: date
    course_end_date: date
    from_department: int
    is_complete: bool

    def as_supabase_payload(self) -> dict[str, Any]:
        return {
            "academic_year": self.academic_year,
            "semester": self.semester,
            "class_room": self.class_room,
            "course_session": self.course_session,
            "course_session_date": self.course_session_date,
            "course_session_duration": self.course_session_duration,
            "course_occurences": self.course_occurences,
            "course_start_date": self.course_start_date.isoformat(),
            "course_end_date": self.course_end_date.isoformat(),
            "from_department": self.from_department,
            "is_complete": self.is_complete,
        }


def _tier_for(code: str) -> str:
    if code in TIER1_CODES:
        return "tier1"
    if code in TIER2_CODES:
        return "tier2"
    return "tier3"


def _department_for(code: str) -> int:
    return DEPT_CS if code in CS_DEPARTMENT_CODES else DEPT_SE


def _classroom_for(index: int) -> str:
    """Map ``index`` to ``A101..A105, B101..B105, …`` (wraps at Z)."""
    letter_idx = (index // ROOMS_PER_LETTER) % 26
    room_idx = index % ROOMS_PER_LETTER
    letter = chr(ord("A") + letter_idx)
    return f"{letter}{ROOM_FIRST_NUMBER + room_idx}"


def _semester_for(index: int) -> int:
    return 1 if index % 2 == 0 else 2


def _session_slot_for(index: int) -> str:
    return SESSION_SLOTS[index % len(SESSION_SLOTS)]


def _day_of_week_for(index: int) -> str:
    return DAYS_OF_WEEK[index % len(DAYS_OF_WEEK)]


def _first_weekday_on_or_after(anchor: date, target_weekday: int) -> date:
    delta = (target_weekday - anchor.weekday()) % 7
    return anchor + timedelta(days=delta)


def _semester_anchor(*, sem1_year: int, sem2_year: int, semester: int) -> date:
    if semester == 1:
        return date(sem1_year, *SEMESTER_1_MONTH_DAY)
    return date(sem2_year, *SEMESTER_2_MONTH_DAY)


def compute_schedule(*, code: str, index: int) -> CourseSchedule:
    """Compute the deterministic schedule for one course."""
    tier_key = _tier_for(code)
    _, academic_year, sem1_year, sem2_year, is_complete, occurences = TIER_BY_KEY[tier_key]

    semester = _semester_for(index)
    day_of_week = _day_of_week_for(index)
    session_slot = _session_slot_for(index)
    classroom = _classroom_for(index)
    department = _department_for(code)

    sem_anchor = _semester_anchor(sem1_year=sem1_year, sem2_year=sem2_year, semester=semester)
    target_weekday = _WEEKDAY_INDEX[day_of_week]
    start = _first_weekday_on_or_after(sem_anchor, target_weekday)
    end = start + timedelta(weeks=occurences - 1)

    return CourseSchedule(
        academic_year=academic_year,
        semester=semester,
        class_room=classroom,
        course_session=session_slot,
        course_session_date=day_of_week,
        course_session_duration=SESSION_DURATION_MIN,
        course_occurences=occurences,
        course_start_date=start,
        course_end_date=end,
        from_department=department,
        is_complete=is_complete,
    )


def schedule_for_code(code: str) -> CourseSchedule | None:
    """Resolve the catalog index for ``code`` and return its schedule.

    Returns ``None`` for codes outside the canonical curriculum so callers
    can fall back to existing behaviour.
    """
    from .catalog import COURSES

    for idx, entry in enumerate(COURSES):
        if entry.code == code:
            return compute_schedule(code=code, index=idx)
    return None


__all__ = [
    "CourseSchedule",
    "compute_schedule",
    "schedule_for_code",
    "TIER_CONFIG",
    "TIER1_CODES",
    "TIER2_CODES",
    "CS_DEPARTMENT_CODES",
    "SESSION_SLOTS",
    "DAYS_OF_WEEK",
    "SESSION_DURATION_MIN",
    "SEMESTER_1_MONTH_DAY",
    "SEMESTER_2_MONTH_DAY",
]
