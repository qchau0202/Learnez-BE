from fastapi import Request, HTTPException

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