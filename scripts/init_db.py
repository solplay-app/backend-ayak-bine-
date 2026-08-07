"""
Crée toutes les tables (users, wallets, transactions, device_tokens) à partir
des modèles SQLAlchemy — pratique pour démarrer rapidement en dev/démo.

En production, remplacer par de vraies migrations Alembic (voir README,
section "Ce qui reste à faire").

Utilisation :
    python -m scripts.init_db
"""
import asyncio

from app.database import Base, engine
from app.models import device_token, models  # noqa: F401  (enregistre les tables sur Base.metadata)


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Tables créées avec succès.")


if __name__ == "__main__":
    asyncio.run(main())
