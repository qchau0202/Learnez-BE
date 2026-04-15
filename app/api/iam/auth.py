from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel, Field
from typing import Optional
from app.core.database import get_supabase
from app.core.dependencies import ROLE_MAP, get_current_user
from supabase_auth.errors import AuthApiError

router = APIRouter(tags=["IAM - Authentication"])
supabase = get_supabase()


def _role_slug(role_id) -> str:
    name = ROLE_MAP.get(role_id)
    if name == "Admin":
        return "admin"
    if name == "Lecturer":
        return "lecturer"
    return "student"


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1)

    model_config = {
        "json_schema_extra": {"example": {"email": "learnez@email.com", "password": "123456"}}
    }


class MeStudentProfileOut(BaseModel):
    student_id: Optional[str] = None
    phone_number: Optional[str] = None
    major: Optional[str] = None
    enrolled_year: Optional[int] = None
    current_gpa: Optional[float] = None
    cumulative_gpa: Optional[float] = None
    gender: Optional[str] = None
    date_of_birth: Optional[str] = None
    faculty_name: Optional[str] = None


class MeLecturerProfileOut(BaseModel):
    lecturer_id: Optional[str] = None
    phone_number: Optional[str] = None
    qualification: Optional[str] = None
    gender: Optional[str] = None
    faculty_name: Optional[str] = None
    faculty_id: Optional[int] = None
    department_name: Optional[str] = None
    department_faculty_id: Optional[int] = None


class MeOut(BaseModel):
    user_id: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    role_id: Optional[int] = None
    role: str
    role_label: str
    is_active: bool = True
    student_profile: Optional[MeStudentProfileOut] = None
    lecturer_profile: Optional[MeLecturerProfileOut] = None


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


def _faculty_name(svc, faculty_id) -> str | None:
    if not faculty_id:
        return None
    fr = svc.table("faculties").select("name").eq("id", faculty_id).limit(1).execute()
    if fr.data:
        return fr.data[0].get("name")
    return None


def _department_info(svc, department_id) -> dict | None:
    if not department_id:
        return None
    dr = (
        svc.table("departments")
        .select("id, name, from_faculty")
        .eq("id", department_id)
        .limit(1)
        .execute()
    )
    if not dr.data:
        return None
    row = dr.data[0]
    faculty_id = row.get("from_faculty")
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "from_faculty": faculty_id,
        "from_faculty_name": _faculty_name(svc, faculty_id),
    }


@router.get(
    "/me",
    summary="Current LMS profile (Bearer access_token)",
    response_model=MeOut,
)
def get_me(user: dict = Depends(get_current_user)):
    """`public.users` plus optional `student_profile` / `lecturer_profile` from the database."""
    user_id = user["user_id"]
    role_id = user.get("role_id")
    base = {
        "user_id": str(user_id),
        "email": user.get("email"),
        "full_name": user.get("full_name"),
        "role_id": role_id,
        "role": _role_slug(role_id),
        "role_label": ROLE_MAP.get(role_id, "Student"),
        "is_active": user.get("is_active", True),
        "student_profile": None,
        "lecturer_profile": None,
    }

    svc = get_supabase(service_role=True)
    if not svc:
        return base

    if role_id == 3:
        sp = svc.table("student_profiles").select("*").eq("user_id", user_id).limit(1).execute()
        if sp.data:
            row = sp.data[0]
            base["student_profile"] = {
                "student_id": row.get("student_id"),
                "phone_number": row.get("phone_number"),
                "major": row.get("major"),
                "enrolled_year": row.get("enrolled_year"),
                "current_gpa": row.get("current_gpa"),
                "cumulative_gpa": row.get("cumulative_gpa"),
                "gender": row.get("gender"),
                "date_of_birth": row.get("date_of_birth"),
                "faculty_name": _faculty_name(svc, row.get("faculty_id")),
            }
    elif role_id == 2:
        lp = svc.table("lecturer_profiles").select("*").eq("user_id", user_id).limit(1).execute()
        if lp.data:
            row = lp.data[0]
            department = _department_info(svc, row.get("department_id"))
            department_faculty_id = department.get("from_faculty") if department else None
            faculty_id = department_faculty_id or row.get("faculty_id")
            base["lecturer_profile"] = {
                "lecturer_id": row.get("lecturer_id"),
                "phone_number": row.get("phone_number"),
                "qualification": row.get("qualification"),
                "gender": row.get("gender"),
                "faculty_name": _faculty_name(svc, faculty_id),
                "faculty_id": faculty_id,
                "department_name": department.get("name") if department else None,
                "department_faculty_id": department_faculty_id,
            }

    return base


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