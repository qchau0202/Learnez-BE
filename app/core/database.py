"""MongoDB and Supabase database connections."""

import os

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


def get_mongo_db():
    client = get_mongo_client()
    settings = get_settings()
    return client[settings["mongodb_db"]]


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