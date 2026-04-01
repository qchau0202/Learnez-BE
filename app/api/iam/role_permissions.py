"""Role-permission management endpoints (admin only)."""

from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.database import get_supabase
from app.core.dependencies import require_roles

router = APIRouter(prefix="/rbac", tags=["IAM - Role Permissions"])

# Stable permission catalog used by UI toggles (id <-> name mapping).
COURSE_PERMISSION_CATALOG = [
    {
        "permission_id": 1,
        "permission_name": "course:title_edit",
        "description": "Edit course title",
    },
    {
        "permission_id": 2,
        "permission_name": "course:title_delete",
        "description": "Delete course title",
    },
    {
        "permission_id": 3,
        "permission_name": "course:module_create",
        "description": "Create module content in a course",
    },
    {
        "permission_id": 4,
        "permission_name": "course:module_edit",
        "description": "Edit module content in a course",
    },
    {
        "permission_id": 5,
        "permission_name": "course:module_delete",
        "description": "Delete module content in a course",
    },
    {
        "permission_id": 6,
        "permission_name": "course:assignment_upload",
        "description": "Upload assignments for a course",
    },
    {
        "permission_id": 7,
        "permission_name": "course:assignment_remove",
        "description": "Remove assignments from a course",
    },
    {
        "permission_id": 8,
        "permission_name": "course:file_upload",
        "description": "Upload course files",
    },
    {
        "permission_id": 9,
        "permission_name": "course:file_remove",
        "description": "Remove course files",
    },
]


class PermissionOut(BaseModel):
    permission_id: int
    permission_name: str
    description: str | None = None


class RoleOut(BaseModel):
    role_id: int
    role_name: str


class SaveRolePermissionsRequest(BaseModel):
    permission_ids: List[int]


class AssignUserRoleRequest(BaseModel):
    role_id: int


class AssignUserRoleByEmailRequest(BaseModel):
    email: str
    role_id: int


class SaveUserPermissionsRequest(BaseModel):
    permission_ids: List[int]


@router.get("/roles", response_model=List[RoleOut])
async def list_roles(user: dict[str, Any] = Depends(require_roles(["Admin"]))):
    supabase = get_supabase(service_role=True)
    res = supabase.table("roles").select("role_id, role_name").order("role_id").execute()
    return res.data or []


@router.get("/permissions", response_model=List[PermissionOut])
async def list_permissions(user: dict[str, Any] = Depends(require_roles(["Admin"]))):
    supabase = get_supabase(service_role=True)
    res = supabase.table("permissions").select("permission_id, permission_name, description").order("permission_id").execute()
    return res.data or []


@router.get("/permission-catalog", response_model=List[PermissionOut])
async def get_permission_catalog(user: dict[str, Any] = Depends(require_roles(["Admin"]))):
    """
    Return predefined permission list with fixed IDs for UI mapping.
    """
    return COURSE_PERMISSION_CATALOG


@router.post("/permissions/sync", response_model=List[PermissionOut], status_code=status.HTTP_200_OK)
async def sync_permission_catalog(user: dict[str, Any] = Depends(require_roles(["Admin"]))):
    """
    Upsert predefined permission catalog into DB so ids/names are stable.
    """
    supabase = get_supabase(service_role=True)

    existing = (
        supabase.table("permissions")
        .select("permission_id, permission_name")
        .in_("permission_id", [p["permission_id"] for p in COURSE_PERMISSION_CATALOG])
        .execute()
    )

    existing_by_id = {row["permission_id"]: row for row in (existing.data or [])}

    inserts = []
    for p in COURSE_PERMISSION_CATALOG:
        if p["permission_id"] in existing_by_id:
            supabase.table("permissions").update(
                {
                    "permission_name": p["permission_name"],
                    "description": p["description"],
                }
            ).eq("permission_id", p["permission_id"]).execute()
        else:
            inserts.append(p)

    if inserts:
        supabase.table("permissions").insert(inserts).execute()

    res = (
        supabase.table("permissions")
        .select("permission_id, permission_name, description")
        .in_("permission_id", [p["permission_id"] for p in COURSE_PERMISSION_CATALOG])
        .order("permission_id")
        .execute()
    )
    return res.data or []


@router.get("/users/{user_id}/permissions", response_model=List[PermissionOut])
async def get_user_permissions(
    user_id: str,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    supabase = get_supabase(service_role=True)
    target = (
        supabase.table("users")
        .select("user_id, role_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not target.data:
        raise HTTPException(status_code=404, detail="User not found")

    # Admin always has full course permissions.
    if target.data[0].get("role_id") == 1:
        return COURSE_PERMISSION_CATALOG

    res = (
        supabase.table("user_permissions")
        .select("permissions(permission_id, permission_name, description)")
        .eq("user_id", user_id)
        .execute()
    )
    permissions = []
    for row in (res.data or []):
        if row.get("permissions"):
            permissions.append(row["permissions"])
    return permissions


@router.put("/users/{user_id}/permissions", status_code=status.HTTP_200_OK)
async def save_user_permissions(
    user_id: str,
    payload: SaveUserPermissionsRequest,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    """Replace all direct permissions for a specific user."""
    supabase = get_supabase(service_role=True)

    target = (
        supabase.table("users")
        .select("user_id, role_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not target.data:
        raise HTTPException(status_code=404, detail="User not found")

    permission_ids = payload.permission_ids
    if target.data[0].get("role_id") == 1:
        permission_ids = [p["permission_id"] for p in COURSE_PERMISSION_CATALOG]

    if permission_ids:
        perms = (
            supabase.table("permissions")
            .select("permission_id")
            .in_("permission_id", permission_ids)
            .execute()
        )
        existing_ids = {p["permission_id"] for p in (perms.data or [])}
        invalid = [pid for pid in permission_ids if pid not in existing_ids]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid permission_id(s): {invalid}")

    supabase.table("user_permissions").delete().eq("user_id", user_id).execute()
    inserts = [{"user_id": user_id, "permission_id": pid} for pid in permission_ids]
    if inserts:
        supabase.table("user_permissions").insert(inserts).execute()

    return {
        "message": "User permissions saved",
        "user_id": user_id,
        "permission_ids": permission_ids
    }


@router.put("/users/{user_id}/role", status_code=status.HTTP_200_OK)
async def assign_role_to_user(
    user_id: str,
    payload: AssignUserRoleRequest,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    """
    Assign a role to a specific user so role_permissions apply to that user.
    """
    supabase = get_supabase(service_role=True)

    role = supabase.table("roles").select("role_id, role_name").eq("role_id", payload.role_id).limit(1).execute()
    if not role.data:
        raise HTTPException(status_code=404, detail="Role not found")

    target_user = supabase.table("users").select("user_id, email").eq("user_id", user_id).limit(1).execute()
    if not target_user.data:
        raise HTTPException(status_code=404, detail="User not found")

    updated = (
        supabase.table("users")
        .update({"role_id": payload.role_id})
        .eq("user_id", user_id)
        .execute()
    )
    if not updated.data:
        raise HTTPException(status_code=500, detail="Failed to assign role")

    return {
        "message": "Role assigned to user",
        "user_id": user_id,
        "role_id": payload.role_id,
        "role_name": role.data[0]["role_name"]
    }


@router.put("/users/assign-role-by-email", status_code=status.HTTP_200_OK)
async def assign_role_to_user_by_email(
    payload: AssignUserRoleByEmailRequest,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    """
    Assign role using user email from lecturer_profiles/student_profiles lookup.
    UI only needs to send email + role_id.
    """
    supabase = get_supabase(service_role=True)

    role = (
        supabase.table("roles")
        .select("role_id, role_name")
        .eq("role_id", payload.role_id)
        .limit(1)
        .execute()
    )
    if not role.data:
        raise HTTPException(status_code=404, detail="Role not found")

    # Correct relationship: email is in users, profiles reference user_id.
    user_row = (
        supabase.table("users")
        .select("user_id, email, role_id")
        .eq("email", payload.email)
        .limit(1)
        .execute()
    )
    if not user_row.data:
        raise HTTPException(status_code=404, detail="User not found by email")

    user_id = user_row.data[0]["user_id"]

    lecturer_profile = (
        supabase.table("lecturer_profiles")
        .select("user_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    student_profile = (
        supabase.table("student_profiles")
        .select("user_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if lecturer_profile.data:
        profile_source = "lecturer_profiles"
    elif student_profile.data:
        profile_source = "student_profiles"
    else:
        raise HTTPException(
            status_code=404,
            detail="User email exists, but no lecturer_profiles/student_profiles record for this user_id"
        )

    updated = (
        supabase.table("users")
        .update({"role_id": payload.role_id})
        .eq("user_id", user_id)
        .execute()
    )
    if not updated.data:
        raise HTTPException(status_code=500, detail="Failed to assign role")

    return {
        "message": "Role assigned to user",
        "email": payload.email,
        "user_id": user_id,
        "role_id": payload.role_id,
        "role_name": role.data[0]["role_name"],
        "resolved_from": profile_source
    }


@router.put("/users/permissions/by-email", status_code=status.HTTP_200_OK)
async def save_user_permissions_by_email(
    payload: SaveUserPermissionsRequest,
    email: str,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    supabase = get_supabase(service_role=True)
    user_row = (
        supabase.table("users")
        .select("user_id")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    if not user_row.data:
        raise HTTPException(status_code=404, detail="User not found by email")

    user_id = user_row.data[0]["user_id"]
    return await save_user_permissions(user_id=user_id, payload=payload, user=user)
