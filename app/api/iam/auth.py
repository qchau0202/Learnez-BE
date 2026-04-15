from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel, Field
from app.core.database import get_supabase
from supabase_auth.errors import AuthApiError

router = APIRouter(tags=["IAM - Authentication"])
supabase = get_supabase()


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1)

    model_config = {
        "json_schema_extra": {"example": {"email": "learnez@email.com", "password": "123456"}}
    }


@router.post("/login", summary="Login with email/password")
def login(data: LoginRequest):
    try:
        res = supabase.auth.sign_in_with_password({
            "email": data.email,
            "password": data.password
        })
    except AuthApiError as e:
        err = e.to_dict()
        code = err.get("code")
        message = err.get("message", "Login failed")

        if code == "email_not_confirmed":
            raise HTTPException(
                status_code=401,
                detail="Email not confirmed. For mock users, mark email as confirmed in Supabase Auth."
            )

        if code in {"invalid_credentials", "invalid_grant"}:
            raise HTTPException(status_code=401, detail="Invalid email or password")

        raise HTTPException(
            status_code=401,
            detail=f"Supabase auth error ({code or 'unknown'}): {message}"
        )
    except Exception:
        raise HTTPException(status_code=500, detail="Login failed")

    return {
        "access_token": res.session.access_token,
        "refresh_token": res.session.refresh_token,
        "user": res.user
    }


@router.post("/bootstrap-admin", summary="Bootstrap first admin profile")
def bootstrap_admin(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=401,
            detail="Missing token. Use header: Authorization: Bearer <access_token>"
        )

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=401,
            detail="Invalid Authorization header. Expected: Bearer <access_token>"
        )
    token = parts[1].strip()

    anon_client = get_supabase()
    user_resp = anon_client.auth.get_user(token)
    if not user_resp.user:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = user_resp.user.id
    email = user_resp.user.email

    service_client = get_supabase(service_role=True)
    if not service_client:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_SERVICE_ROLE_KEY")

    # One-time bootstrap guard: only allow this if no admin exists yet.
    existing_admin = service_client.table("users").select("user_id").eq("role_id", 1).limit(1).execute()
    existing_self = service_client.table("users").select("user_id, role_id").eq("user_id", user_id).limit(1).execute()

    if existing_self.data:
        role_id = existing_self.data[0].get("role_id")
        return {
            "message": "Profile already exists",
            "user_id": user_id,
            "email": email,
            "role_id": role_id
        }

    if existing_admin.data:
        raise HTTPException(
            status_code=403,
            detail="Bootstrap disabled: an admin already exists. Ask existing admin to create your account."
        )

    created = service_client.table("users").insert({
        "user_id": user_id,
        "email": email,
        "role_id": 1,
        "is_active": True
    }).execute()

    if not created.data:
        raise HTTPException(status_code=500, detail="Failed to create admin profile")

    return {
        "message": "Admin profile bootstrapped successfully",
        "user_id": user_id,
        "email": email,
        "role_id": 1
    }