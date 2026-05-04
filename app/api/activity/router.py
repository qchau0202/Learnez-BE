"""Activity module router."""

from fastapi import APIRouter

from app.api.activity import activity, analytics, simulation

router = APIRouter(tags=["Activity & AI"])

router.include_router(activity.router)
router.include_router(analytics.router)
router.include_router(simulation.router)
