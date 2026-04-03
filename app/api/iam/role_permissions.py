"""Role-permission management endpoints (admin only)."""

from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.database import get_supabase
from app.core.dependencies import require_roles

router = APIRouter(prefix="/rbac", tags=["IAM - Role Permissions"])

# Permission names are code-like for stable frontend mapping.
PERMISSION_CATALOG = [
    {"permission_id": 1, "permission_name": "course-01", "description": "Create course"},
    {"permission_id": 2, "permission_name": "course-02", "description": "View course"},
    {"permission_id": 3, "permission_name": "course-03", "description": "Update course"},
    {"permission_id": 4, "permission_name": "course-04", "description": "Delete course"},

    {"permission_id": 5, "permission_name": "module-01", "description": "Create course modules"},
    {"permission_id": 6, "permission_name": "module-02", "description": "View course modules"},
    {"permission_id": 7, "permission_name": "module-03", "description": "Update course modules"},
    {"permission_id": 8, "permission_name": "module-04", "description": "Delete course modules"},

    {"permission_id": 9, "permission_name": "material-01", "description": "Create module materials"},
    {"permission_id": 10, "permission_name": "material-02", "description": "View module materials"},
    {"permission_id": 11, "permission_name": "material-03", "description": "Update module materials"},
    {"permission_id": 12, "permission_name": "material-04", "description": "Delete module materials"},

    {"permission_id": 13, "permission_name": "assignment-01", "description": "Create assignments"},
    {"permission_id": 14, "permission_name": "assignment-02", "description": "View assignments"},
    {"permission_id": 15, "permission_name": "assignment-03", "description": "Update assignments"},
    {"permission_id": 16, "permission_name": "assignment-04", "description": "Delete assignments"},
]

ADMIN_FULL_PERMISSION_IDS = [p["permission_id"] for p in PERMISSION_CATALOG]
LECTURER_DEFAULT_PERMISSION_IDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13]


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


class UserPermissionOverrideItem(BaseModel):
    permission_id: int
    is_allowed: bool


class SaveUserPermissionOverridesRequest(BaseModel):
    overrides: List[UserPermissionOverrideItem]


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
    return PERMISSION_CATALOG


@router.post("/permissions/sync", response_model=List[PermissionOut], status_code=status.HTTP_200_OK)
async def sync_permission_catalog(user: dict[str, Any] = Depends(require_roles(["Admin"]))):
    supabase = get_supabase(service_role=True)

    existing = (
        supabase.table("permissions")
        .select("permission_id, permission_name")
        .in_("permission_id", [p["permission_id"] for p in PERMISSION_CATALOG])
        .execute()
    )

    existing_by_id = {row["permission_id"]: row for row in (existing.data or [])}

    inserts = []
    for p in PERMISSION_CATALOG:
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
        .in_("permission_id", [p["permission_id"] for p in PERMISSION_CATALOG])
        .order("permission_id")
        .execute()
    )

    # Rebuild role-permission defaults:
    # - Any role containing "admin" gets full permissions.
    # - Lecturer gets lecturer default permissions.
    roles = supabase.table("roles").select("role_id, role_name").execute().data or []
    for role in roles:
        role_id = role["role_id"]
        role_name = (role.get("role_name") or "").strip().lower()

        if "admin" in role_name:
            role_perm_ids = ADMIN_FULL_PERMISSION_IDS
        elif role_name == "lecturer":
            role_perm_ids = LECTURER_DEFAULT_PERMISSION_IDS
        else:
            role_perm_ids = []

        supabase.table("role_permissions").delete().eq("role_id", role_id).execute()
        if role_perm_ids:
            supabase.table("role_permissions").insert(
                [{"role_id": role_id, "permission_id": pid} for pid in role_perm_ids]
            ).execute()

    return res.data or []


@router.get("/roles/{role_id}/permissions", response_model=List[PermissionOut])
async def get_role_permissions(
    role_id: int,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    supabase = get_supabase(service_role=True)

    role = supabase.table("roles").select("role_id").eq("role_id", role_id).limit(1).execute()
    if not role.data:
        raise HTTPException(status_code=404, detail="Role not found")

    res = (
        supabase.table("role_permissions")
        .select("permissions(permission_id, permission_name, description)")
        .eq("role_id", role_id)
        .execute()
    )
    permissions = []
    for row in (res.data or []):
        if row.get("permissions"):
            permissions.append(row["permissions"])
    return permissions


@router.put("/roles/{role_id}/permissions", status_code=status.HTTP_200_OK)
async def save_role_permissions(
    role_id: int,
    payload: SaveRolePermissionsRequest,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    supabase = get_supabase(service_role=True)
    role = supabase.table("roles").select("role_id, role_name").eq("role_id", role_id).limit(1).execute()
    if not role.data:
        raise HTTPException(status_code=404, detail="Role not found")

    role_name = (role.data[0].get("role_name") or "").strip().lower()
    permission_ids = payload.permission_ids
    if "admin" in role_name:
        permission_ids = ADMIN_FULL_PERMISSION_IDS

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

    supabase.table("role_permissions").delete().eq("role_id", role_id).execute()
    if permission_ids:
        supabase.table("role_permissions").insert(
            [{"role_id": role_id, "permission_id": pid} for pid in permission_ids]
        ).execute()

    return {"message": "Role permissions saved", "role_id": role_id, "permission_ids": permission_ids}


@router.put("/users/{user_id}/role", status_code=status.HTTP_200_OK)
async def assign_role_to_user(
    user_id: str,
    payload: AssignUserRoleRequest,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
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


@router.get("/users/{user_id}/permissions", response_model=List[PermissionOut])
async def get_user_permissions(
    user_id: str,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    supabase = get_supabase(service_role=True)
    user_row = (
        supabase.table("users")
        .select("user_id, role_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not user_row.data:
        raise HTTPException(status_code=404, detail="User not found")

    role_id = user_row.data[0]["role_id"]
    role_permissions = (
        supabase.table("role_permissions")
        .select("permissions(permission_id, permission_name, description)")
        .eq("role_id", role_id)
        .execute()
    )
    default_permissions = []
    for row in (role_permissions.data or []):
        if row.get("permissions"):
            default_permissions.append(row["permissions"])

    effective_ids = {p["permission_id"] for p in default_permissions}
    overrides = (
        supabase.table("user_permissions")
        .select("permission_id, is_allowed")
        .eq("user_id", user_id)
        .execute()
    )
    for row in (overrides.data or []):
        pid = row["permission_id"]
        if row.get("is_allowed") is True:
            effective_ids.add(pid)
        elif row.get("is_allowed") is False and pid in effective_ids:
            effective_ids.remove(pid)

    if not effective_ids:
        return []

    permissions = (
        supabase.table("permissions")
        .select("permission_id, permission_name, description")
        .in_("permission_id", list(effective_ids))
        .order("permission_id")
        .execute()
    )
    return permissions.data or []


@router.get("/users/{user_id}/permission-overrides", status_code=status.HTTP_200_OK)
async def get_user_permission_overrides(
    user_id: str,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    supabase = get_supabase(service_role=True)
    target = (
        supabase.table("users")
        .select("user_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not target.data:
        raise HTTPException(status_code=404, detail="User not found")

    rows = (
        supabase.table("user_permissions")
        .select("permission_id, is_allowed, changed_by")
        .eq("user_id", user_id)
        .order("permission_id")
        .execute()
    )
    return {"user_id": user_id, "overrides": rows.data or []}


@router.put("/users/{user_id}/permission-overrides", status_code=status.HTTP_200_OK)
async def save_user_permission_overrides(
    user_id: str,
    payload: SaveUserPermissionOverridesRequest,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    """
    Replace all user-level permission overrides.
    - is_allowed=True adds/grants a permission
    - is_allowed=False revokes a permission granted by role default
    """
    supabase = get_supabase(service_role=True)
    target = (
        supabase.table("users")
        .select("user_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not target.data:
        raise HTTPException(status_code=404, detail="User not found")

    permission_ids = [o.permission_id for o in payload.overrides]
    if permission_ids:
        existing = (
            supabase.table("permissions")
            .select("permission_id")
            .in_("permission_id", permission_ids)
            .execute()
        )
        existing_ids = {p["permission_id"] for p in (existing.data or [])}
        invalid = [pid for pid in permission_ids if pid not in existing_ids]
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid permission_id(s): {invalid}")

    admin_user_id = user["user_id"]
    supabase.table("user_permissions").delete().eq("user_id", user_id).execute()
    if payload.overrides:
        supabase.table("user_permissions").insert(
            [
                {
                    "user_id": user_id,
                    "permission_id": o.permission_id,
                    "is_allowed": o.is_allowed,
                    "changed_by": admin_user_id,
                }
                for o in payload.overrides
            ]
        ).execute()

    return {
        "message": "User permission overrides saved",
        "user_id": user_id,
        "changed_saved_by": admin_user_id,
        "overrides": [
            {
                "permission_id": o.permission_id,
                "is_allowed": o.is_allowed,
                "changed_by": admin_user_id,
            }
            for o in payload.overrides
        ],
    }


@router.put("/users/permission-overrides/by-email", status_code=status.HTTP_200_OK)
async def save_user_permission_overrides_by_email(
    email: str,
    payload: SaveUserPermissionOverridesRequest,
    user: dict[str, Any] = Depends(require_roles(["Admin"]))
):
    supabase = get_supabase(service_role=True)
    target = (
        supabase.table("users")
        .select("user_id")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    if not target.data:
        raise HTTPException(status_code=404, detail="User not found by email")

    return await save_user_permission_overrides(
        user_id=target.data[0]["user_id"],
        payload=payload,
        user=user,
    )
