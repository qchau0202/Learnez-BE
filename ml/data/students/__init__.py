"""Student-side seeders: accounts, course content, behaviour, demo data.

Module map (each module also runs as a CLI via ``python -m
ml.data.students.<module>``):

* ``provision`` — create the ~50 demo student accounts (idempotent).
* ``features``  — weekly behavioural snapshots in ``student_weekly_features``.
* ``attendance`` / ``submissions`` — per-student Supabase fact tables.
* ``content``   — modules + assignments + questions per course.
* ``behaviour`` — Mongo risk_scores, competency_profiles, raw events.
* ``cohort``    — bulk uniform seeder across the whole demo cohort.
* ``student1``  — orchestrator that wires all of the above into a
  polished, fluctuative profile for the primary demo account.
* ``diagnose``  — read-only audit across Supabase + Mongo for one user.
"""

from .attendance import seed_attendance_for_student
from .behaviour import seed_behaviour_and_risk
from .content import provision_content_for_courses
from .features import PERSONAS, seed_student_features
from .submissions import seed_submissions_for_student

__all__ = [
    "PERSONAS",
    "provision_content_for_courses",
    "seed_attendance_for_student",
    "seed_behaviour_and_risk",
    "seed_student_features",
    "seed_submissions_for_student",
]
