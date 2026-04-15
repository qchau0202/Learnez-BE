import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
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

app = FastAPI(
    title="Learnez Backend API",
    description=(
        "FastAPI backend for LMS domains (IAM, Courses, Assessment, Storage, Activity). "
        "Use these docs for manual API testing and integration."
    ),
    version="1.0.0",
    openapi_tags=tags_metadata,
)

app.add_middleware(AuthMiddleware)

app.include_router(router)


@app.get("/")
def root():
    return {"message": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}