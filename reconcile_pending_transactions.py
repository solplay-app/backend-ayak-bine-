"""
Réconciliation manuelle des transactions bloquées en PENDING parce que le
webhook JEKO n'a jamais pu être livré (cas typique : service Render endormi
au moment où JEKO a tenté ses 3 rappels).

Ce script interroge JEKO DIRECTEMENT (source de vérité) pour connaître le
vrai statut de chaque étape (pay-in / pay-out) d'une transaction PENDING,
puis rejoue exactement la même logique métier que le webhook réel
(app/services/webhook_service.py) : versement automatique du pay-out si le
pay-in est confirmé, recrédit du wallet si le pay-out a échoué, etc.

SÉCURITÉ / IDEMPOTENCE :
  - Chaque étape (payin_status / payout_status) n'est traitée que si elle
    est encore PENDING dans notre base. Si le vrai webhook finit par arriver
    en parallèle (ou est arrivé juste avant que ce script tourne), le check
    `in TERMINAL_LEG_STATUSES` dans webhook_service empêche tout double
    traitement (double versement, double recrédit).
  - Rien n'est modifié tant que --apply n'est pas passé explicitement.
    Par défaut le script tourne en DRY-RUN et affiche seulement ce qu'il
    ferait.
  - Un `--min-age-minutes` (5 par défaut) évite de toucher des transactions
    encore légitimement en cours (client vient de payer il y a 10 secondes).

Utilisation :
    # 1. Toujours commencer par un dry-run pour voir ce qui sera fait
    python -m scripts.reconcile_pending_transactions

    # 2. Une fois vérifié, appliquer réellement les corrections
    python -m scripts.reconcile_pending_transactions --apply

    # Cibler une seule transaction précise (recommandé pour un cas isolé
    # comme "les 2 transferts visibles dans l'historique de l'app") :
    python -m scripts.reconcile_pending_transactions --apply --reference TRF-ABCD1234
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import AsyncSessionLocal, get_redis
from app.models.models import Transaction, TransactionStatus, TransactionType
from app.services.jeko_client import JekoAPIError, JekoClient, JekoNetworkError
from app.services.webhook_service import _handle_payin_webhook, _handle_payout_webhook  # noqa: SLF001

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reconcile")


def _map_jeko_status(raw_status: str | None) -> TransactionStatus | None:
    """None = toujours en attente côté JEKO, on ne touche à rien."""
    if raw_status is None:
        return None
    value = raw_status.lower()
    if value in ("success", "completed", "successful"):
        return TransactionStatus.SUCCESS
    if value in ("error", "failed", "cancelled", "canceled", "rejected"):
        return TransactionStatus.FAILED
    return None  # "pending", "processing", etc. -> on ne fait rien pour l'instant


async def _reconcile_one(db, jeko: JekoClient, transaction: Transaction, *, apply: bool) -> None:
    ref = transaction.internal_reference
    action_prefix = "[APPLY]" if apply else "[DRY-RUN]"

    # --- Étape pay-in (uniquement pertinent pour un TRANSFER) ---
    if transaction.type == TransactionType.TRANSFER and transaction.payin_status == TransactionStatus.PENDING:
        if not transaction.jeko_payin_id:
            logger.warning("%s %s : payin_status PENDING mais aucun jeko_payin_id -> ignoré (jamais initié côté JEKO)", ref)
        else:
            try:
                jeko_data = await jeko.get_payment_request_status(transaction.jeko_payin_id)
            except (JekoAPIError, JekoNetworkError) as exc:
                logger.error("%s : échec vérification pay-in auprès de JEKO (%s)", ref, exc)
                return
            new_status = _map_jeko_status(jeko_data.get("status"))
            if new_status is None:
                logger.info("%s : pay-in toujours PENDING côté JEKO, rien à faire", ref)
            else:
                logger.info(
                    "%s %s : pay-in réellement %s côté JEKO (était PENDING en base)",
                    action_prefix, ref, new_status.value,
                )
                if apply:
                    await _handle_payin_webhook(
                        db, transaction, new_status, jeko_event_id="reconcile-script", jeko=jeko
                    )
                    await db.commit()
                    logger.info("%s : pay-in traité. Nouveau statut global = %s", ref, transaction.status.value)
                return  # on retraite au prochain passage du script pour le pay-out si besoin

    # --- Étape pay-out (TRANSFER une fois le pay-in confirmé, ou WITHDRAWAL) ---
    if transaction.payout_status == TransactionStatus.PENDING:
        if not transaction.jeko_payout_id:
            logger.warning("%s : payout_status PENDING mais aucun jeko_payout_id -> ignoré (jamais initié côté JEKO)", ref)
            return
        try:
            jeko_data = await jeko.get_transfer_status(transaction.jeko_payout_id)
        except (JekoAPIError, JekoNetworkError) as exc:
            logger.error("%s : échec vérification pay-out auprès de JEKO (%s)", ref, exc)
            return
        new_status = _map_jeko_status(jeko_data.get("status"))
        if new_status is None:
            logger.info("%s : pay-out toujours PENDING côté JEKO, rien à faire", ref)
            return

        logger.info(
            "%s %s : pay-out réellement %s côté JEKO (était PENDING en base)",
            action_prefix, ref, new_status.value,
        )
        if apply:
            await _handle_payout_webhook(db, transaction, new_status)
            await db.commit()
            logger.info("%s : pay-out traité. Nouveau statut global = %s", ref, transaction.status.value)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Applique réellement les corrections (sinon dry-run).")
    parser.add_argument("--min-age-minutes", type=int, default=5, help="Ignore les transactions plus récentes que N minutes.")
    parser.add_argument("--reference", type=str, default=None, help="Ne traiter qu'une seule référence interne précise (ex: TRF-ABCD1234).")
    args = parser.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.min_age_minutes)

    async with AsyncSessionLocal() as db:
        jeko = JekoClient()
        redis = get_redis()  # non utilisé directement ici, mais garde la parité d'init avec le reste de l'app
        try:
            if args.reference:
                stmt = select(Transaction).where(Transaction.internal_reference == args.reference)
            else:
                stmt = select(Transaction).where(
                    Transaction.status == TransactionStatus.PENDING,
                    Transaction.created_at <= cutoff,
                )
            result = await db.execute(stmt)
            transactions = result.scalars().all()

            if not transactions:
                logger.info("Aucune transaction PENDING à réconcilier (avec les filtres donnés).")
                return

            logger.info("%d transaction(s) à examiner. Mode = %s", len(transactions), "APPLY" if args.apply else "DRY-RUN")

            for transaction in transactions:
                await _reconcile_one(db, jeko, transaction, apply=args.apply)

            if not args.apply:
                logger.info("Dry-run terminé. Relancez avec --apply pour appliquer réellement ces corrections.")
        finally:
            await jeko.close()


if __name__ == "__main__":
    asyncio.run(main())
