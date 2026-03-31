from fastapi import APIRouter
from pydantic import BaseModel
from app.core.database import get_supabase

router = APIRouter()
supabase = get_supabase()


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
def login(data: LoginRequest):

    res = supabase.auth.sign_in_with_password({
        "email": data.email,
        "password": data.password
    })

    return {
        "access_token": res.session.access_token,
        "refresh_token": res.session.refresh_token,
        "user": res.user
    }