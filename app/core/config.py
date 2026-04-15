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
        "cloudinary_cloud_name": os.getenv("CLOUDINARY_CLOUD_NAME", ""),
        "cloudinary_api_key": os.getenv("CLOUDINARY_API_KEY", ""),
        "cloudinary_api_secret": os.getenv("CLOUDINARY_API_SECRET", ""),
        "student_storage_quota_mb": int(os.getenv("STUDENT_STORAGE_QUOTA_MB", "500")),
        "student_file_max_mb": int(os.getenv("STUDENT_FILE_MAX_MB", "25")),
        "jwt_secret": os.getenv("JWT_SECRET", "change-me-in-production"),
        "jwt_algorithm": os.getenv("JWT_ALGORITHM", "HS256"),
        "jwt_expire_minutes": int(os.getenv("JWT_EXPIRE_MINUTES", "60")),
    }
