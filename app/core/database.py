"""MongoDB and Supabase database connections."""

from motor.motor_asyncio import AsyncIOMotorClient
from supabase import create_client, Client

from app.core.config import get_settings


def get_mongo_client() -> AsyncIOMotorClient:
    settings = get_settings()
    return AsyncIOMotorClient(settings["mongodb_uri"])


def get_mongo_db():
    client = get_mongo_client()
    settings = get_settings()
    return client[settings["mongodb_db"]]


def get_supabase_client() -> Client | None:
    settings = get_settings()
    if settings["supabase_url"] and settings["supabase_anon_key"]:
        return create_client(settings["supabase_url"], settings["supabase_anon_key"])
    return None
