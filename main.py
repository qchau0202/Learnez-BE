"""E-Learning LMS Backend - FastAPI entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.database import get_mongo_client, get_mongo_db, get_supabase_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.mongo = get_mongo_client()
    app.state.db = get_mongo_db()
    app.state.supabase = get_supabase_client()
    yield
    app.state.mongo.close()


app = FastAPI(
    title="E-Learning LMS API",
    description="Backend for e-learning system with IAM, Courses, Assessment, Activity Tracking, and AI features",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(api_router, tags=["API v1"])


@app.get("/")
async def root():
    return {"message": "E-learning API ready", "status": "ok"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
