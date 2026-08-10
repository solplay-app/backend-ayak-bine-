from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import admin, auth, devices, transfers, wallet, webhooks
from app.database import get_redis
from app.services.jeko_client import get_jeko_client


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
