"""Curriculum data — catalog, schedule, crawler, Supabase sync."""

from .catalog import COURSES, CurriculumCourse, codes, title_for
from .schedule import CourseSchedule, compute_schedule, schedule_for_code

__all__ = [
    "COURSES",
    "CourseSchedule",
    "CurriculumCourse",
    "codes",
    "compute_schedule",
    "schedule_for_code",
    "title_for",
]
