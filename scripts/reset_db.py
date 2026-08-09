"""
⚠️ SUPPRIME PUIS RECRÉE TOUTES LES TABLES — usage ponctuel uniquement.

Contrairement à scripts/init_db.py (qui ne crée que les tables manquantes,
sans jamais toucher aux tables existantes), ce script commence par TOUT
supprimer. À utiliser uniquement quand la base contient encore l'ancien
schéma (avant la restructuration "agent") et qu'aucune donnée réelle n'a
besoin d'être conservée.

⚠️ Important : on supprime le schéma PostgreSQL entier (DROP SCHEMA ...
CASCADE) plutôt que d'utiliser Base.metadata.drop_all(). Base.metadata ne
connaît que les tables définies dans le code ACTUEL (app/models/*) : si la
base contient encore d'anciennes tables d'une version précédente (ex:
"clients", "agents" du modèle v1) avec des clés étrangères vers des tables
toujours présentes en v2 (ex: "users"), drop_all() échoue avec
DependentObjectsStillExistError car il ignore ces tables orphelines et ne
sait pas qu'il doit aussi les supprimer. Recréer le schéma en entier évite
ce problème une fois pour toutes.

Déclenché uniquement si la variable d'environnement RUN_RESET_DB=true
(volontairement différente de RUN_INIT_DB, pour ne jamais l'exécuter par
erreur lors d'un déploiement normal une fois la base en production).

Utilisation :
    python -m scripts.reset_db
"""
import asyncio

from sqlalchemy import text

from app.database import Base, engine
from app.models import device_token, models  # noqa: F401  (enregistre les tables sur Base.metadata)


async def main() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        print("🗑️  Ancien schéma entièrement supprimé (y compris tables orphelines d'anciennes versions).")
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Tables recréées avec le nouveau schéma v2 (transfert inter-opérateurs).")


if __name__ == "__main__":
    asyncio.run(main())
