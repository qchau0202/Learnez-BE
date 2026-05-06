"""Application configuration from environment variables."""

import os
from functools import lru_cache
from pathlib import Path


def _load_local_env_files() -> None:
    """Load the first `.env` file found in common workspace locations.

    This keeps the application usable in local development without requiring
    explicit shell exports. Existing environment variables always win.
    """

    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / ".env"
        if not candidate.exists():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)
        break


_load_local_env_files()


@lru_cache
def get_settings():
    mongodb_db = os.getenv("MONGODB_DB", "elearning")
    return {
        "mongodb_uri": os.getenv("MONGODB_URI", "mongodb://localhost:27017"),
        "mongodb_db": mongodb_db,
        "mongodb_raw_db": os.getenv("MONGODB_RAW_DB", f"{mongodb_db}_raw"),
        "mongodb_ai_db": os.getenv("MONGODB_AI_DB", mongodb_db),
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
