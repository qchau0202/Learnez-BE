"""RBAC table models for Supabase-backed IAM."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Role(BaseModel):
    role_id: int
    role_name: str = Field(min_length=1, max_length=100)


class Permission(BaseModel):
    permission_id: int
    permission_name: str = Field(min_length=1, max_length=150)
    description: Optional[str] = None


class RolePermission(BaseModel):
    role_id: int
    permission_id: int
    created_at: Optional[datetime] = None


class UserRoleAssignment(BaseModel):
    user_id: str
    role_id: int
