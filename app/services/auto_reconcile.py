"""
Filet de sécurité automatique pour les transactions PENDING (TRANSFER ou
WITHDRAWAL).

Problème résolu : sur le plan gratuit Render, l'app peut s'endormir juste
après qu'un client initie un transfert. Si JEKO envoie son webhook pendant
ce court instant d'endormissement, les 3 tentatives de webhook (backoff
court côté JEKO) peuvent toutes échouer, et la transaction reste bloquée
à PENDING indéfiniment côté client — jusqu'à une intervention manuelle
via app/api/v1/admin.py.

Ce module supprime le besoin d'intervention manuelle dans le cas courant :
dès qu'une transaction est créée, on programme une vérification automatique
du VRAI statut auprès de JEKO (source de vérité) après quelques minutes.
Si un vrai webhook arrive entre-temps, cette vérification ne fait rien
(la transaction n'est déjà plus PENDING) — aucun risque de double
traitement, la logique de app/services/reconciliation.py est idempotente.

Fonctionne comme une simple tâche asyncio en arrière-plan dans le même
processus (pas besoin de Cron Job, donc compatible plan gratuit Render).
Limite : si Render endort complètement l'app avant l'échéance des 5-11
minutes ET qu'aucune requête ne la réveille entre-temps, la tâche meurt
avec le process. Dans ce cas rare, la route admin de secours reste
disponible. Un passage au plan payant Render (Cron Job fiable) reste la
solution long terme recommandée.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.models import Transaction, TransactionStatus
from app.services.jeko_client import get_jeko_client
from app.services.reconciliation import TransactionNotFoundForReconciliation, reconcile_transaction_by_reference

logger = logging.getLogger("auto_reconcile")

TERMINAL_STATUSES = {
    TransactionStatus.SUCCESS,
    TransactionStatus.FAILED,
    TransactionStatus.FAILED_PAYOUT,
}

# Premier essai à 5 min (laisse le temps à un vrai webhook d'arriver si
# l'app était juste endormie), puis 2 relances à 3 min d'intervalle pour
# couvrir le cas TRANSFER où le pay-in vient de se débloquer et le
# pay-out a besoin, lui aussi, d'un peu de temps.
_RETRY_DELAYS_SECONDS = [300, 180, 180]

# Garde une référence forte vers les tâches en cours pour éviter qu'elles
# soient supprimées prématurément par le garbage collector (piège classique
# d'asyncio.create_task).
_background_tasks: set[asyncio.Task] = set()


def schedule_auto_reconcile(internal_reference: str) -> None:
    """À appeler juste après avoir committé une transaction PENDING fraîchement créée."""
    task = asyncio.create_task(_auto_reconcile_loop(internal_reference))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _auto_reconcile_loop(internal_reference: str) -> None:
    jeko = get_jeko_client()

    for delay in _RETRY_DELAYS_SECONDS:
        await asyncio.sleep(delay)
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Transaction).where(Transaction.internal_reference == internal_reference)
                )
                transaction = result.scalar_one_or_none()
                if transaction is None or transaction.status in TERMINAL_STATUSES:
                    return

                report = await reconcile_transaction_by_reference(db, jeko, internal_reference, apply=True)
                logger.info("Auto-réconciliation %s: %s", internal_reference, report["actions"])
        except TransactionNotFoundForReconciliation:
            return
        except Exception:  # noqa: BLE001 - ne doit jamais faire planter le process
            logger.exception("Auto-réconciliation: échec inattendu pour %s", internal_reference)
