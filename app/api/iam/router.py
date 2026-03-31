from fastapi import APIRouter
from app.api.iam.auth import router as auth_router
from app.api.iam.accounts import router as accounts_router

router = APIRouter()

router.include_router(auth_router, prefix="/iam")
router.include_router(accounts_router, prefix="/iam")