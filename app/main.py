"""
Assemblage FastAPI : instancie l'application et branche tous les routers.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.api.v1 import admin, auth, devices, transfers, wallet, webhooks

# Note : le module app/api/v1/agent.py correspond à l'ancien modèle
# "agence Mobile Money" (agent gérant des clients en cash-in/cash-out).
# Il n'est plus branché ici depuis le passage à un modèle client-à-client
# (transfert inter-réseau direct, sans agent). Le fichier reste dans le
# repo mais n'est pas importé, et n'a jamais été terminé (modèle `Client`
# et plusieurs fonctions de service manquants) — à supprimer ou reprendre
# plus tard si le modèle agence revient un jour.

app = FastAPI(title="Ayak'bine — Backend Wallet")

app.include_router(auth.router)
app.include_router(wallet.router)
app.include_router(transfers.router)
app.include_router(devices.router)
app.include_router(webhooks.router)
app.include_router(admin.router)


@app.get("/health", tags=["Health"])
async def health() -> dict[str, str]:
    """Utilisé par Render (`render.yaml` -> healthCheckPath) pour vérifier que le service est en vie."""
    return {"status": "ok"}
