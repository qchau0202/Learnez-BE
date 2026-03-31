import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from app.middlewares.middleware import AuthMiddleware
from app.api.router import router

app = FastAPI()

app.add_middleware(AuthMiddleware)

app.include_router(router)


@app.get("/")
def root():
    return {"message": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}