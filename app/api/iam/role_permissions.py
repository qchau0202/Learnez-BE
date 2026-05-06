"""Role-permission management endpoints (admin only)."""

from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.database import get_supabase
from app.core.dependencies import require_roles

router = APIRouter(prefix="/rbac", tags=["IAM - Role Permissions"])

# Permission names are code-like for stable frontend mapping.
# NOTE: The role mapping policy is strict for non-admin roles:
# - Admin: full access
# - Lecturer: teaching + grading + support views (no account mgmt, no course create/delete)
# - Student: learning workflow + personal/report views (no authoring/grading)
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
# Lecturer defaults (strict to mapping table):
# - Course lifecycle: view/update only (no create/delete)
# - Manage materials / assignments
# - Grading support
LECTURER_DEFAULT_PERMISSION_IDS = [2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]

# Student defaults (strict to mapping table):
# - Read-only course/module/material access
# - Assignment learning workflow access (view/submit handled by role-protected submission endpoints)
# - No authoring / grading permissions
STUDENT_DEFAULT_PERMISSION_IDS = [2, 6, 10, 14]

# Non-admin roles must stay inside these sets even when admin edits roles/overrides.
ROLE_POLICY_PERMISSION_IDS = {
    "admin": set(ADMIN_FULL_PERMISSION_IDS),
    "lecturer": set(LECTURER_DEFAULT_PERMISSION_IDS),
    "student": set(STUDENT_DEFAULT_PERMISSION_IDS),
}


def _normalized_role_name(raw: str | None) -> str:
    return (raw or "").strip().lower()


def _policy_permission_ids_for_role(role_name: str) -> set[int]:
    name = _normalized_role_name(role_name)
    if "admin" in name:
        return ROLE_POLICY_PERMISSION_IDS["admin"]
    return ROLE_POLICY_PERMISSION_IDS.get(name, set())


class PermissionOut(BaseModel):
    permission_id: int
    permission_name: str
    description: str | None = None


class RoleOut(BaseModel):
    role_id: int
    role_name: str


class AssignUserRoleRequest(BaseModel):
    role_id: int

    model_config = {"json_schema_extra": {"example": {"role_id": 2}}}


class AssignUserRoleByEmailRequest(BaseModel):
    email: str
    role_id: int

    model_config = {"json_schema_extra": {"example": {"email": "student1@email.com", "role_id": 3}}}


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

    # Rebuild role-permission defaults using strict policy.
    roles = supabase.table("roles").select("role_id, role_name").execute().data or []
    for role in roles:
        role_id = role["role_id"]
        role_name = _normalized_role_name(role.get("role_name"))
        role_perm_ids = sorted(_policy_permission_ids_for_role(role_name))

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

    role_effective_ids = {p["permission_id"] for p in default_permissions}
    if not role_effective_ids:
        return []

    permissions = (
        supabase.table("permissions")
        .select("permission_id, permission_name, description")
        .in_("permission_id", list(role_effective_ids))
        .order("permission_id")
        .execute()
    )
    return permissions.data or []
