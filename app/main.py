"""Point d'entrée FastAPI."""
import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api_admin import router as admin_router
from app.api_auth import router as auth_router
from app.api_devices import router as devices_router
from app.api_kyc import router as kyc_router
from app.api_wallet import router as wallet_router
from app.api_webhooks import router as webhooks_router
from app.config import get_settings
from app.database import _sync_engine
from app.db_migrate import run_migrations

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("startup")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Démarrage Ayak'bine Wallet — host=%s port=%s", settings.APP_HOST, settings.APP_PORT)
    try:
        run_migrations(_sync_engine)
        logger.info("Migrations SQL à jour.")
    except Exception:
        logger.exception("Échec de l'application des migrations SQL — arrêt du service.")
        raise
    yield


app = FastAPI(
    title="Ayak'bine Virtual Wallet",
    version="1.1.0",
    description="Backend FastAPI pour Pay-In / Pay-Out via Virtual Ledger.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"ok": True, "service": "ayak-wallet"}


app.include_router(auth_router)
app.include_router(wallet_router)
app.include_router(webhooks_router)
app.include_router(admin_router)
app.include_router(kyc_router)
app.include_router(devices_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.APP_HOST, port=settings.APP_PORT, reload=True)
