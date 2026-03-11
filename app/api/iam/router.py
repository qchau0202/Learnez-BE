"""IAM module router."""

from fastapi import APIRouter

from app.api.iam import auth, accounts

router = APIRouter(tags=["IAM"])

router.include_router(auth.router)
router.include_router(accounts.router)
