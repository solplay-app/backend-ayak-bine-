"""
⚠️ SUPPRIME PUIS RECRÉE TOUTES LES TABLES — usage ponctuel uniquement.

Contrairement à scripts/init_db.py (qui ne crée que les tables manquantes,
sans jamais toucher aux tables existantes), ce script commence par TOUT
supprimer. À utiliser uniquement quand la base contient encore l'ancien
schéma (avant la restructuration "agent") et qu'aucune donnée réelle n'a
besoin d'être conservée.

Déclenché uniquement si la variable d'environnement RUN_RESET_DB=true
(volontairement différente de RUN_INIT_DB, pour ne jamais l'exécuter par
erreur lors d'un déploiement normal une fois la base en production).

Utilisation :
    python -m scripts.reset_db
"""
import asyncio

from app.database import Base, engine
from app.models import device_token, models  # noqa: F401  (enregistre les tables sur Base.metadata)


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        print("🗑️  Anciennes tables supprimées.")
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Tables recréées avec le nouveau schéma agent.")


if __name__ == "__main__":
    asyncio.run(main())
