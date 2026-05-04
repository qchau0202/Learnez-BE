"""MongoDB and Supabase database connections."""

from __future__ import annotations

from functools import lru_cache

from motor.motor_asyncio import AsyncIOMotorClient
from supabase import Client, create_client

from app.core.config import get_settings

try:
    import certifi

    _MONGO_TLS = {"tlsCAFile": certifi.where()}
except ImportError:
    _MONGO_TLS = {}


def get_mongo_client() -> AsyncIOMotorClient:
    settings = get_settings()
    return AsyncIOMotorClient(settings["mongodb_uri"], **_MONGO_TLS)


def get_mongo_db(db_name: str | None = None):
    client = get_mongo_client()
    settings = get_settings()
    selected_db = db_name or settings["mongodb_db"]
    return client[selected_db]


def get_mongo_raw_db():
    settings = get_settings()
    return get_mongo_db(settings["mongodb_raw_db"])


def get_mongo_ai_db():
    settings = get_settings()
    return get_mongo_db(settings["mongodb_ai_db"])


def get_supabase_client() -> Client | None:
    settings = get_settings()
    if settings["supabase_url"] and settings["supabase_anon_key"]:
        return create_client(settings["supabase_url"], settings["supabase_anon_key"])
    return None


def get_supabase(service_role: bool = False):
    settings = get_settings()
    if service_role and settings["supabase_service_key"]:
        return create_client(settings["supabase_url"], settings["supabase_service_key"])
    return get_supabase_client()