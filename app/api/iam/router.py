from fastapi import APIRouter
from app.api.iam.router import router as iam_router

router = APIRouter()

router.include_router(iam_router, prefix="/api")