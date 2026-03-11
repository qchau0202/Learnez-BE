"""Account Management - Admin creates Lecturer/Student accounts."""

from fastapi import APIRouter, Depends

from app.api.deps import DbDep

router = APIRouter(prefix="/accounts", tags=["IAM - Account Management"])


@router.post("/")
async def create_account(db: DbDep):
    """Admin: Create account for Lecturer or Student."""
    ...


@router.get("/")
async def list_accounts(db: DbDep):
    """Admin: List all accounts with filters (role, search)."""
    ...


@router.get("/{account_id}")
async def get_account(db: DbDep, account_id: str):
    """Get account by ID."""
    ...
