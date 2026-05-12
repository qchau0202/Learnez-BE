"""Canonical course list for the TDTU Software Engineering curriculum.

Single source of truth used by the crawler, sync script, and seeders.
Course codes are opaque strings — never coerce to int (some contain
letters, e.g. ``"512CM6"``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CurriculumCourse:
    code: str
    title_en: str


# Ordered to follow the curriculum sequence (math → CS core → tracks →
# capstone). Order is not load-bearing — Supabase assigns its own id.
COURSES: tuple[CurriculumCourse, ...] = (
    CurriculumCourse("501031", "Applied Calculus for IT"),
    CurriculumCourse("501032", "Applied Linear Algebra for IT"),
    CurriculumCourse("501042", "Programming Methodology"),
    CurriculumCourse("501044", "Discrete Structures"),
    CurriculumCourse("502044", "Computer Organisation"),
    CurriculumCourse("502045", "Software Engineering"),
    CurriculumCourse("502046", "Introduction to Computer Networks"),
    CurriculumCourse("502047", "Introduction to Operating Systems"),
    CurriculumCourse("502048", "Introduction to Media Computing"),
    CurriculumCourse("502049", "Introduction to Information Security"),
    CurriculumCourse("502050", "Requirements Analysis and Design"),
    CurriculumCourse("502051", "Database Systems"),
    CurriculumCourse("502052", "Enterprise Systems Development Concepts"),
    CurriculumCourse("502061", "Applied Probability and Statistics for IT"),
    CurriculumCourse("502065", "Computer Network Programming"),
    CurriculumCourse("502067", "Game Graphic Design"),
    CurriculumCourse("502068", "IoT Fundamentals"),
    CurriculumCourse("502070", "Web Application Development Using NodeJS"),
    CurriculumCourse("502071", "Cross-Platform Mobile Application Development"),
    CurriculumCourse("502072", "Automated Software Testing"),
    CurriculumCourse("502090", "Graduation Internship"),
    CurriculumCourse("502093", "Full-stack Software Development"),
    CurriculumCourse("502094", "Software Deployment, Operations and Maintenance"),
    CurriculumCourse("502095", "Advanced Software Engineering"),
    CurriculumCourse("502097", "Advanced Database Systems"),
    CurriculumCourse("503005", "Object-Oriented Programming"),
    CurriculumCourse("503040", "Design and Analysis of Algorithms"),
    CurriculumCourse("503043", "Introduction to Artificial Intelligence"),
    CurriculumCourse("503044", "Introduction to Machine Learning"),
    CurriculumCourse("503066", "Enterprise Resource Planning Systems"),
    CurriculumCourse("503073", "Web Programming and Applications"),
    CurriculumCourse("503074", "Mobile Apps Development"),
    CurriculumCourse("503080", "Introduction to Computer Vision"),
    CurriculumCourse("503103", "IoT Security"),
    CurriculumCourse("503108", "UI/UX Design"),
    CurriculumCourse("503109", "Management of Information Systems"),
    CurriculumCourse("503111", "Java Technology"),
    CurriculumCourse("503112", ".Net Technology"),
    CurriculumCourse("503116", "Introduction to Logical Thinking"),
    CurriculumCourse("503117", "Designing Machine Learning Systems"),
    CurriculumCourse("504008", "Data Structures And Algorithms"),
    CurriculumCourse("504048", "Massive Data Processing"),
    CurriculumCourse("504049", "Business Intelligence Systems"),
    CurriculumCourse("504058", "Software Testing"),
    CurriculumCourse("504070", "Enterprise Service-Oriented Architecture"),
    CurriculumCourse("504074", "Industrial Experience Requirement"),
    CurriculumCourse("504076", "Game Development"),
    CurriculumCourse("504077", "Design Pattern"),
    CurriculumCourse("504087", "Cloud Computing"),
    CurriculumCourse("504088", "Introduction to Computer Security"),
    CurriculumCourse("504091", "Information Technology Project"),
    CurriculumCourse("504093", "Cloud Security"),
    CurriculumCourse("504101", "Network Attack and Defense"),
    CurriculumCourse("504105", "Time Series Analysis and Forecasting"),
    CurriculumCourse("505009", "Project Management"),
    CurriculumCourse("505010", "Typography of Technical Documents"),
    CurriculumCourse("505011", "Functional Programming"),
    CurriculumCourse("505012", "History of Science and Technology"),
    CurriculumCourse("505043", "Knowledge Discovery and Data Mining"),
    CurriculumCourse("505060", "Introduction to Digital Image Processing"),
    CurriculumCourse("505063", "Blockchain and Distributed Ledger Technologies"),
    CurriculumCourse("505065", "Embedded System Programming"),
    CurriculumCourse("512CM6", "Professional Skills Exam"),
)


def codes() -> list[str]:
    """Return raw course codes in curriculum order."""
    return [c.code for c in COURSES]


def title_for(code: str) -> str | None:
    """Look up the canonical English title for a course code."""
    for c in COURSES:
        if c.code == code:
            return c.title_en
    return None


__all__ = ["CurriculumCourse", "COURSES", "codes", "title_for"]
