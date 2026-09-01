"""
Application entry point.

The API is the thin half of this service -- it adds and removes watches. The
work happens in the Celery workers, which run the same code from the same
image but never import this module.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import health, sections, watches

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("seatalerts")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail at boot rather than looking healthy while being unable to deliver.
    settings.validate_production_safety()
    logger.info(
        "class seat alerts starting in %s (telegram=%s, email=%s)",
        settings.app_env,
        settings.telegram_enabled,
        settings.email_enabled,
    )
    yield
    logger.info("class seat alerts shutting down")


app = FastAPI(
    title="Class Seat Alerts",
    description=(
        "Watches class sections on the university course schedule and sends a "
        "Telegram or email alert the moment a full section opens -- once per "
        "closed-to-open transition, never twice."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

if settings.cors_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(health.router)
app.include_router(watches.router)
app.include_router(sections.router)
