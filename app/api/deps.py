"""Shared API dependencies: DB access, auth, RBAC checks."""

from typing import Annotated

from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_mongo_db


async def get_db() -> AsyncIOMotorDatabase:
    return get_mongo_db()


# Type alias for dependency injection
DbDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]
