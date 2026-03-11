"""Application configuration from environment variables."""

import os
from functools import lru_cache


@lru_cache
def get_settings():
    return {
        "mongodb_uri": os.getenv("MONGODB_URI", "mongodb://localhost:27017"),
        "mongodb_db": os.getenv("MONGODB_DB", "elearning"),
        "supabase_url": os.getenv("SUPABASE_URL", ""),
        "supabase_anon_key": os.getenv("SUPABASE_ANON_KEY", ""),
        "supabase_service_key": os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        "jwt_secret": os.getenv("JWT_SECRET", "change-me-in-production"),
        "jwt_algorithm": os.getenv("JWT_ALGORITHM", "HS256"),
        "jwt_expire_minutes": int(os.getenv("JWT_EXPIRE_MINUTES", "60")),
    }
