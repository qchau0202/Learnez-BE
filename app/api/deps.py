"""Shared API dependencies: DB access, auth, RBAC checks."""

from typing import Annotated

from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_mongo_ai_db, get_mongo_db, get_mongo_raw_db


async def get_db() -> AsyncIOMotorDatabase:
    return get_mongo_db()


async def get_ai_db() -> AsyncIOMotorDatabase:
    return get_mongo_ai_db()


async def get_raw_db() -> AsyncIOMotorDatabase:
    return get_mongo_raw_db()


# Type alias for dependency injection
DbDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]
AiDbDep = Annotated[AsyncIOMotorDatabase, Depends(get_ai_db)]
RawDbDep = Annotated[AsyncIOMotorDatabase, Depends(get_raw_db)]
