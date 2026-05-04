from typing import Any

from fastapi import HTTPException, Request

from app.core.database import get_supabase

ROLE_MAP = {
    1: "Admin",
    2: "Lecturer",
    3: "Student"
}


def get_current_user(request: Request):
    user = getattr(request.state, "user", None)

    if not user:
        raise HTTPException(status_code=401, detail="Unauthenticated")

    return user


def require_roles(roles: list):
    def checker(request: Request):
        user = getattr(request.state, "user", None)

        if not user:
            raise HTTPException(status_code=401, detail="Unauthenticated")

        role_name = ROLE_MAP.get(user["role_id"])

        if role_name not in roles:
            raise HTTPException(status_code=403, detail="Forbidden")

        return user

    return checker


def _effective_permission_names(user_id: str) -> set[str]:
    supabase = get_supabase(service_role=True)
    if not supabase:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")

    user_row = (
        supabase.table("users")
        .select("user_id, role_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not user_row.data:
        raise HTTPException(status_code=401, detail="Unauthenticated")

    role_id = user_row.data[0].get("role_id")
    effective: set[str] = set()

    role_permissions = (
        supabase.table("role_permissions")
        .select("permissions(permission_name)")
        .eq("role_id", role_id)
        .execute()
    )
    for row in role_permissions.data or []:
        perm = row.get("permissions")
        if isinstance(perm, dict):
            name = perm.get("permission_name")
            if name:
                effective.add(str(name))

    overrides = (
        supabase.table("user_permissions")
        .select("permission_id, is_allowed, permissions(permission_name)")
        .eq("user_id", user_id)
        .execute()
    )
    for row in overrides.data or []:
        perm = row.get("permissions")
        if not isinstance(perm, dict):
            continue
        name = perm.get("permission_name")
        if not name:
            continue
        permission_name = str(name)
        if row.get("is_allowed") is True:
            effective.add(permission_name)
        elif row.get("is_allowed") is False:
            effective.discard(permission_name)

    return effective


def user_has_permissions(user: dict[str, Any], required_permissions: list[str]) -> bool:
    if not required_permissions:
        return True
    return set(required_permissions).issubset(_effective_permission_names(user["user_id"]))


def require_permissions(required_permissions: list[str]):
    required = [perm for perm in required_permissions if perm]

    def checker(request: Request):
        user = getattr(request.state, "user", None)
        if not user:
            raise HTTPException(status_code=401, detail="Unauthenticated")

        effective = _effective_permission_names(user["user_id"])
        missing = [perm for perm in required if perm not in effective]
        if missing:
            raise HTTPException(status_code=403, detail="Forbidden")

        return user

    return checker