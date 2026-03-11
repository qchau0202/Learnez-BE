"""Authentication endpoints - Login, Logout."""

from fastapi import APIRouter, Depends

from app.api.deps import DbDep

router = APIRouter(prefix="/auth", tags=["IAM - Authentication"])


@router.post("/login")
async def login(db: DbDep):
    """Login with email and password. Returns JWT token."""
    ...


@router.post("/logout")
async def logout():
    """Logout (invalidate token / clear session)."""
    ...
