import logging
import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.middlewares.middleware import AuthMiddleware
from app.api.router import router
from app.services.ai.chat_sessions import ensure_chat_indexes

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
async def _ensure_chat_indexes() -> None:
    """Create chat-session indexes on every boot.

    Idempotent — Mongo silently no-ops if the index already exists.
    Keeping it here means a fresh deployment doesn't have to remember
    to run ``ml/data/mongodb_bootstrap.py`` before the chatbot works.
    """
    try:
        await ensure_chat_indexes()
    except Exception as exc:
        # Don't crash the API on a Mongo hiccup at boot — the chat
        # endpoint will surface a real error if Mongo is genuinely down.
        logger.warning("Failed to ensure chat indexes at startup: %s", exc)


@app.get("/")
def root():
    return {"message": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}