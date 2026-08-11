from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1 import admin, auth, devices, kyc, transfers, wallet, webhooks
from app.database import Base, engine, get_redis
from app.models.models import KycStatus, MobileOperator, TransactionStatus, TransactionType
from app.services.jeko_client import get_jeko_client

# Sans ceci, le logger racine reste au niveau WARNING par défaut : tous les
# logger.info(...) de l'app (otp_service, sms.console, sms.twilio, ...) sont
# silencieusement ignorés. Seuls les logs d'accès d'uvicorn (health, requêtes
# HTTP) apparaissent, car uvicorn configure ses propres loggers séparément.
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

logger = logging.getLogger("startup")

# Mapping nom du type Postgres -> Enum Python correspondant (voir app/models/models.py).
# Sert à combler automatiquement les valeurs manquantes côté base au démarrage,
# indispensable sur le plan gratuit Render qui ne donne pas accès au Shell/psql.
_PG_ENUMS = {
    "transaction_type": TransactionType,
    "transaction_status": TransactionStatus,
    "mobile_operator": MobileOperator,
    "kyc_status": KycStatus,
}


async def _sync_enum_values(conn) -> None:
    """
    create_all() ne modifie JAMAIS un type enum Postgres déjà existant : si un
    membre est ajouté à un Enum Python après coup (ex: TransactionType.DEPOSIT),
    la base ne le connaît pas tant qu'on ne fait pas explicitement
    ALTER TYPE ... ADD VALUE. Sans accès Shell (plan gratuit Render), on le fait
    ici, au démarrage de l'app, plutôt que via psql.
    """
    for pg_type_name, py_enum in _PG_ENUMS.items():
        for member in py_enum:
            # ADD VALUE IF NOT EXISTS : idempotent, ne casse rien si déjà présent.
            await conn.execute(text(f"ALTER TYPE {pg_type_name} ADD VALUE IF NOT EXISTS '{member.value}'"))
            logger.info("Enum %s: valeur '%s' vérifiée/ajoutée", pg_type_name, member.value)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Crée automatiquement les tables manquantes (ex: kyc_submissions) au
    # démarrage — sans ça il faudrait un accès Shell (indisponible sur le
    # plan gratuit Render) pour créer la nouvelle table manuellement.
    # Sans danger pour les tables déjà existantes : create_all ne touche
    # jamais une table qui existe déjà, ni ses colonnes.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Transaction séparée : depuis Postgres 12, ALTER TYPE ... ADD VALUE
    # fonctionne dans un bloc transactionnel classique (plus besoin d'autocommit).
    async with engine.begin() as conn:
        await _sync_enum_values(conn)

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
app.include_router(kyc.router)
