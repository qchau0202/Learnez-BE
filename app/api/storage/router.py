"""Storage module router."""

from fastapi import APIRouter

from app.api.storage import storage

router = APIRouter(tags=["Storage"])

router.include_router(storage.router)
