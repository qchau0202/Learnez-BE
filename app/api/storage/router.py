"""Storage module router."""

from fastapi import APIRouter

from app.api.storage import storage, student_files_routes

router = APIRouter(tags=["Storage"])

router.include_router(storage.router)
router.include_router(student_files_routes.router)
