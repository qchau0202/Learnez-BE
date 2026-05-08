"""Search module router."""

from fastapi import APIRouter

from app.api.search.global_search import router as global_search_router

router = APIRouter(tags=["Search"])

router.include_router(global_search_router)
