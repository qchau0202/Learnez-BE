import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.middlewares.middleware import AuthMiddleware
from app.api.router import router

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


@app.get("/")
def root():
    return {"message": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}