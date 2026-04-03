from fastapi import APIRouter
from app.api.iam.auth import router as auth_router
from app.api.iam.accounts import router as accounts_router
from app.api.iam.role_permissions import router as role_permissions_router

router = APIRouter()

router.include_router(auth_router, prefix="/iam")
router.include_router(accounts_router, prefix="/iam")
router.include_router(role_permissions_router, prefix="/iam")
