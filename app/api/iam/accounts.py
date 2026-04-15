"""Account Management - Admin creates Lecturer/Student accounts."""

from fastapi import APIRouter, Depends, HTTPException, status, Request
from typing import List, Optional
from pydantic import BaseModel, Field

from app.core.dependencies import require_roles
from app.core.database import get_supabase

router = APIRouter(prefix="/accounts", tags=["IAM - Account Management"])

# Example Pydantic models

class AccountCreate(BaseModel):
    email: str
    password: str = Field(min_length=6)
    role_id: int = Field(description="1=Admin, 2=Lecturer, 3=Student")

    model_config = {
        "json_schema_extra": {
            "example": {
                "email": "student1@email.com",
                "password": "123456",
                "role_id": 3,
            }
        }
    }


class AccountOut(BaseModel):
    id: str
    email: str
    role_id: int


@router.post("/", response_model=AccountOut, status_code=status.HTTP_201_CREATED, summary="Create account")
async def create_account(
    account: AccountCreate,
    request: Request,
    user=Depends(require_roles(["Admin"]))
):
    # Use service role for admin operations
    supabase = get_supabase(service_role=True)

    try:
        # 1. Create user in Supabase Auth (admin)
        auth_res = supabase.auth.admin.create_user({
            "email": account.email,
            "password": account.password,
            "email_confirm": True
        })

        if not auth_res.user:
            raise HTTPException(status_code=400, detail="Cannot create auth user")

        user_id = auth_res.user.id

        # 2. Insert into users table
        db_res = supabase.table("users").insert({
            "user_id": user_id,
            "email": account.email,
            "role_id": account.role_id,
            "is_active": True,
            "created_by": user["user_id"]
        }).execute()

        if not db_res.data:
            raise HTTPException(status_code=400, detail="Insert DB failed")

        # 3. Insert into profile table based on role
        if account.role_id == 2:
            # Lecturer
            supabase.table("lecturer_profiles").insert({
                "user_id": user_id,
                # Add more lecturer profile fields as needed
            }).execute()
        elif account.role_id == 3:
            # Student
            supabase.table("student_profiles").insert({
                "user_id": user_id,
                # Add more student profile fields as needed
            }).execute()

        return {
            "id": user_id,
            "email": account.email,
            "role_id": account.role_id
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/", response_model=List[AccountOut], summary="List accounts")
async def list_accounts(
    request: Request,
    role_id: Optional[int] = None,
    user=Depends(require_roles(["Admin"]))
):
    supabase = get_supabase(service_role=True)

    query = supabase.table("users").select("*")

    if role_id:
        query = query.eq("role_id", role_id)

    res = query.execute()

    return [
        {
            "id": u["user_id"],
            "email": u["email"],
            "role_id": u["role_id"]
        }
        for u in res.data
    ]

@router.get("/{account_id}", response_model=AccountOut, summary="Get account by user_id")
async def get_account(
    account_id: str,
    user=Depends(require_roles(["Admin"]))
):
    supabase = get_supabase(service_role=True)

    res = supabase.table("users") \
        .select("*") \
        .eq("user_id", account_id) \
        .execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="User not found")

    u = res.data[0]

    return {
        "id": u["user_id"],
        "email": u["email"],
        "role_id": u["role_id"]
    }

@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete account")
async def delete_account(
    account_id: str,
    user=Depends(require_roles(["Admin"]))
):
    supabase = get_supabase(service_role=True)

    supabase.table("users") \
        .delete() \
        .eq("user_id", account_id) \
        .execute()

    return