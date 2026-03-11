"""Course module router."""

from fastapi import APIRouter

from app.api.course import courses, content, enrollment

router = APIRouter(tags=["Course & Content"])

router.include_router(courses.router)
router.include_router(content.router)
router.include_router(enrollment.router)
