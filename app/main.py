from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import admin, auth, devices, transfers, wallet, webhooks
from app.database import get_redis
from app.services.jeko_client import get_jeko_client


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    root_logger = logging.getLogger()

    if not root_logger.handlers:
        logging.basicConfig(level=level, format=log_format)
    else:
        root_logger.setLevel(level)
        formatter = logging.Formatter(log_format)
        for handler in root_logger.handlers:
            handler.setLevel(level)
            if handler.formatter is None:
                handler.setFormatter(formatter)

    logging.getLogger("sms.console").setLevel(level)
    logging.getLogger("otp_service").setLevel(level)


configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    redis = get_redis()
    await redis.close()
    await get_jeko_client().close()


app = FastAPI(title="Ayak'bine Backend", version="2.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"ok": True}


app.include_router(auth.router)
app.include_router(wallet.router)
app.include_router(transfers.router)
app.include_router(webhooks.router)
app.include_router(devices.router)
app.include_router(admin.router)
