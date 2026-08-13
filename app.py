"""Minimal FastAPI foundation for the remediation orchestrator."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from store import close_connection, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        yield
    finally:
        close_connection()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook/github")
async def webhook_github():
    return JSONResponse(
        status_code=501,
        content={"message": "Webhook processing is not implemented yet."},
    )
