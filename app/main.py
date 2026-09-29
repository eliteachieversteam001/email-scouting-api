"""Email Scouting API — MailClean functionality on Elite Firebase identity.

Every route except /health requires a verified Elite Firebase ID token
(see app/auth.py). There are no signup, login, password or user-admin
endpoints: accounts and member status live in Elite.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import ensure_indexes
from app.routes import dashboard, leads, verify

log = logging.getLogger("email_scouting")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    ensure_indexes()
    yield


def create_app(*, init_db: bool = True) -> FastAPI:
    app = FastAPI(
        title="Email Scouting API",
        version="3.0.0",
        lifespan=lifespan if init_db else None,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=False,  # bearer tokens only, never cookies
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Content-Disposition"],
        max_age=600,
    )

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Never leak stack traces or database errors to clients.
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": "3.0.0"}

    app.include_router(verify.router)
    app.include_router(leads.router)
    app.include_router(dashboard.router)
    return app


app = create_app()
