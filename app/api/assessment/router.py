"""Assessment module router."""

from fastapi import APIRouter

from app.api.assessment import attendance, assignments, grading, notifications

router = APIRouter(tags=["Learning & Assessment"])

router.include_router(attendance.router)
router.include_router(assignments.router)
router.include_router(grading.router)
router.include_router(notifications.router)
