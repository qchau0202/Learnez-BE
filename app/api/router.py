"""API router - aggregates all module routers."""

from fastapi import APIRouter

from app.api.iam.router import router as iam_router
from app.api.course.router import router as course_router
from app.api.assessment.router import router as assessment_router
from app.api.activity.router import router as activity_router
from app.api.storage.router import router as storage_router

api_router = APIRouter(prefix="/api")

api_router.include_router(iam_router)
api_router.include_router(course_router)
api_router.include_router(assessment_router)
api_router.include_router(activity_router)
api_router.include_router(storage_router)

# Backward-compatible export for main.py
router = api_router
