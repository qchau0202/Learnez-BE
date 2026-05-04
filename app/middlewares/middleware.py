from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request
from starlette.responses import JSONResponse
from app.core.database import get_supabase

supabase_anon = get_supabase()
supabase_service = get_supabase(service_role=True)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # CORS preflight must not require Authorization
        if request.method == "OPTIONS":
            return await call_next(request)

        public_paths = {
            "/",
            "/health",
            "/api/iam/login",
            "/api/iam/bootstrap-admin",
            "/api/activity/sim/ingest-batch",
            "/docs",
            "/redoc",
            "/openapi.json",
        }

        # Public routes
        if request.url.path in public_paths:
            return await call_next(request)

        auth_header = request.headers.get("Authorization")

        if not auth_header:
            return JSONResponse(
                status_code=401,
                content={"message": "Missing token. Use header: Authorization: Bearer <access_token>"}
            )

        try:
            parts = auth_header.split(" ", 1)
            if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
                return JSONResponse(
                    status_code=401,
                    content={"message": "Invalid Authorization header. Expected: Bearer <access_token>"}
                )
            token = parts[1].strip()

            # Verify Supabase token
            user_resp = supabase_anon.auth.get_user(token)

            if not user_resp.user:
                return JSONResponse(status_code=401, content={"message": "Invalid token"})

            user_id = user_resp.user.id

            # Get user from public.users table
            if not supabase_service:
                return JSONResponse(
                    status_code=500,
                    content={"message": "Server misconfiguration: missing SUPABASE_SERVICE_ROLE_KEY"}
                )

            db_user = supabase_service.table("users") \
                .select("*") \
                .eq("user_id", user_id) \
                .execute()

            if not db_user.data:
                return JSONResponse(
                    status_code=401,
                    content={
                        "message": "Token is valid, but this auth user has no profile in public.users. "
                                   "Insert a row in public.users with user_id=<auth_user_id> and role_id."
                    }
                )

            request.state.user = db_user.data[0]

        except Exception:
            return JSONResponse(
                status_code=401,
                content={"message": "Authentication failed. Token may be invalid/expired or Supabase auth check failed."}
            )

        return await call_next(request)