"""Authentication and authorization utilities."""

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Placeholder - implement JWT when auth is ready
security = HTTPBearer(auto_error=False)


class Role(str, Enum):
    ADMIN = "admin"
    LECTURER = "lecturer"
    STUDENT = "student"


# Permission constants for RBAC
class Permission:
    # Course & Content
    COURSE_CREATE = "course:create"
    COURSE_EDIT = "course:edit"
    COURSE_DELETE = "course:delete"
    COURSE_VIEW = "course:view"
    CONTENT_UPLOAD = "content:upload"
    ENROLLMENT_MANAGE = "enrollment:manage"

    # Assessment
    ATTENDANCE_TAKE = "attendance:take"
    ATTENDANCE_VIEW = "attendance:view"
    ASSIGNMENT_CREATE = "assignment:create"
    ASSIGNMENT_GRADE = "assignment:grade"
    ASSIGNMENT_VIEW = "assignment:view"

    # Accounts (Admin only)
    ACCOUNT_CREATE = "account:create"
    ACCOUNT_VIEW = "account:view"


def get_role_permissions(role: Role) -> set[str]:
    """Map roles to permissions for RBAC."""
    role_perms = {
        Role.ADMIN: {
            Permission.COURSE_CREATE,
            Permission.COURSE_EDIT,
            Permission.COURSE_DELETE,
            Permission.COURSE_VIEW,
            Permission.CONTENT_UPLOAD,
            Permission.ENROLLMENT_MANAGE,
            Permission.ATTENDANCE_TAKE,
            Permission.ATTENDANCE_VIEW,
            Permission.ASSIGNMENT_CREATE,
            Permission.ASSIGNMENT_GRADE,
            Permission.ASSIGNMENT_VIEW,
            Permission.ACCOUNT_CREATE,
            Permission.ACCOUNT_VIEW,
        },
        Role.LECTURER: {
            Permission.COURSE_EDIT,
            Permission.COURSE_VIEW,
            Permission.CONTENT_UPLOAD,
            Permission.ATTENDANCE_TAKE,
            Permission.ATTENDANCE_VIEW,
            Permission.ASSIGNMENT_CREATE,
            Permission.ASSIGNMENT_GRADE,
            Permission.ASSIGNMENT_VIEW,
        },
        Role.STUDENT: {
            Permission.COURSE_VIEW,
            Permission.ATTENDANCE_VIEW,
            Permission.ASSIGNMENT_VIEW,
        },
    }
    return role_perms.get(role, set())
