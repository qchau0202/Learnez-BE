from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request
from starlette.responses import JSONResponse
from app.core.database import get_supabase

supabase = get_supabase()


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):

        # Public routes
        if request.url.path in ["/", "/health", "/api/iam/login"]:
            return await call_next(request)

        auth_header = request.headers.get("Authorization")

        if not auth_header:
            return JSONResponse(status_code=401, content={"message": "Missing token"})

        try:
            token = auth_header.split(" ")[1]

            # 🔥 Verify Supabase token
            user_resp = supabase.auth.get_user(token)

            if not user_resp.user:
                return JSONResponse(status_code=401, content={"message": "Invalid token"})

            user_id = user_resp.user.id

            # 🔥 Lấy user từ bảng public.users
            db_user = supabase.table("users") \
                .select("*") \
                .eq("user_id", user_id) \
                .execute()

            if not db_user.data:
                return JSONResponse(status_code=401, content={"message": "User not found"})

            request.state.user = db_user.data[0]

        except Exception as e:
            return JSONResponse(status_code=401, content={"message": "Auth error"})

        return await call_next(request)