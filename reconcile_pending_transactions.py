"""
Réconciliation manuelle en ligne de commande (nécessite un accès Shell —
sur le plan gratuit Render, utilise plutôt la route admin HTTP :
voir app/api/v1/admin.py, utilisable en collant une URL dans le navigateur).

Utilisation :
    python -m scripts.reconcile_pending_transactions --reference TRF-ABCD1234
    python -m scripts.reconcile_pending_transactions --reference TRF-ABCD1234 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.database import AsyncSessionLocal
from app.services.jeko_client import JekoClient
from app.services.reconciliation import (
    TransactionNotFoundForReconciliation,
    reconcile_transaction_by_reference,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reconcile")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=str, required=True, help="Référence interne, ex: TRF-ABCD1234")
    parser.add_argument("--apply", action="store_true", help="Applique réellement les corrections (sinon dry-run).")
    args = parser.parse_args()

    async with AsyncSessionLocal() as db:
        jeko = JekoClient()
        try:
            try:
                report = await reconcile_transaction_by_reference(db, jeko, args.reference, apply=args.apply)
            except TransactionNotFoundForReconciliation:
                logger.error("Transaction '%s' introuvable.", args.reference)
                return

            for action in report["actions"]:
                logger.info("%s", action)
            logger.info("Statut : %s -> %s", report["status_before"], report["status_after"])
        finally:
            await jeko.close()


if __name__ == "__main__":
    asyncio.run(main())
