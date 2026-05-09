import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.middlewares.middleware import AuthMiddleware
from app.api.router import router
from app.core.database import get_mongo_ai_db, get_mongo_raw_db

logger = logging.getLogger(__name__)

tags_metadata = [
    {"name": "IAM - Authentication", "description": "Login and bootstrap endpoints."},
    {"name": "IAM - Account Management", "description": "Admin-only account CRUD for Lecturer/Student profiles."},
    {"name": "IAM - Role Permissions", "description": "Role and permission management APIs."},
    {"name": "Course & Content - Courses", "description": "Course and module management."},
    {"name": "Course & Content - Enrollment", "description": "Enroll/unenroll students to courses."},
    {"name": "Course & Content - Content", "description": "Module material upload/update/delete APIs."},
    {"name": "Learning - Assignments", "description": "Assignment, question, and submission lifecycle endpoints."},
    {"name": "Learning - Grading", "description": "Manual grading and feedback APIs."},
    {"name": "Learning - Notifications", "description": "Manual and scenario/job-based notifications."},
    {"name": "Learning - Attendance", "description": "Attendance endpoints (placeholder for now)."},
]

_cors_origins = [o.strip() for o in (os.getenv("CORS_ORIGINS") or "").split(",") if o.strip()]
if not _cors_origins:
    _cors_origins = [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

app = FastAPI(
    title="Learnez Backend API",
    description=(
        "FastAPI backend for LMS domains (IAM, Courses, Assessment, Storage, Activity). "
        "Use these docs for manual API testing and integration."
    ),
    version="1.0.0",
    openapi_tags=tags_metadata,
)

# Auth wraps the app first; CORS is added last so it is outermost and can attach headers to all responses.
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.on_event("startup")
async def _prewarm_mongo() -> None:
    """Open the Mongo Atlas connection pool *before* the first user
    request lands.

    Without this the very first request after boot pays the full TLS
    + cluster discovery handshake (~10–15 s on free Atlas tiers),
    which makes the app feel broken on a cold start. We fire a cheap
    ``ping`` against both DBs in the background so the connection
    pool is hot when the FE starts polling.

    Failures are logged, not raised — Mongo can recover later and we
    don't want a transient cloud hiccup at boot to take the whole API
    down.
    """

    async def _ping() -> None:
        try:
            await asyncio.gather(
                get_mongo_ai_db().command("ping"),
                get_mongo_raw_db().command("ping"),
            )
            logger.info("Mongo connection pool prewarmed.")
        except Exception as exc:
            logger.warning("Mongo prewarm failed (will retry on demand): %s", exc)

    # Run as a background task so startup completes immediately and the
    # health check passes even while the prewarm is still in flight.
    asyncio.create_task(_ping())


@app.get("/")
def root():
    return {"message": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}