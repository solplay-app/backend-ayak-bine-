import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import agent, auth, devices, webhooks
from app.services.jeko_client import get_jeko_client
from app.services.sms import close_sms_provider

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Ayak'bine — Plateforme Agent Mobile Money",
    description="Backend Agent (Cash-In / Cash-Out / Commissions) - Intégration JEKO Africa",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # à restreindre en production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(agent.router)
app.include_router(webhooks.router)


@app.get("/health", tags=["Monitoring"])
async def health_check():
    return {"status": "ok"}


@app.on_event("shutdown")
async def shutdown_event():
    await get_jeko_client().close()
    await close_sms_provider()
