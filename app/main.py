"""
Assemblage FastAPI : instancie l'application et branche tous les routers.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.api.v1 import admin, agent, auth, devices, transfers, wallet, webhooks

app = FastAPI(title="Ayak'bine — Backend Wallet")

app.include_router(auth.router)
app.include_router(wallet.router)
app.include_router(transfers.router)
app.include_router(devices.router)
app.include_router(webhooks.router)
app.include_router(agent.router)
app.include_router(admin.router)


@app.get("/health", tags=["Health"])
async def health() -> dict[str, str]:
    """Utilisé par Render (`render.yaml` -> healthCheckPath) pour vérifier que le service est en vie."""
    return {"status": "ok"}
