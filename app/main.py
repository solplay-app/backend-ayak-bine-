import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import admin, auth, devices, transfers, wallet, webhooks
from app.services.jeko_client import get_jeko_client
from app.services.sms import close_sms_provider

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Ayak'bine — Transfert interopérable Mobile Money",
    description="Wave ⇄ Orange Money ⇄ MTN MoMo ⇄ Moov Money, sans agent ni cash — Intégration JEKO Africa",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # à restreindre en production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(wallet.router)
app.include_router(transfers.router)
app.include_router(webhooks.router)


@app.get("/health", tags=["Monitoring"])
async def health_check():
    return {"status": "ok"}


@app.on_event("shutdown")
async def shutdown_event():
    await get_jeko_client().close()
    await close_sms_provider()
