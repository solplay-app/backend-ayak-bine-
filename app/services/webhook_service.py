"""
Traitement idempotent des webhooks JEKO.

Deux niveaux de protection contre les doublons (JEKO peut renvoyer le même
webhook plusieurs fois en cas de timeout réseau de son côté) :
  1. Verrou Redis court-terme sur `jeko_transaction_id` (anti double-traitement
     concurrent, ex: 2 webhooks reçus à 200ms d'intervalle).
  2. Vérification en base : si la transaction est déjà dans un état terminal
     (SUCCESS/FAILED/CANCELLED), le webhook est acquitté (200) mais ignoré.
"""
from __future__ import annotations

import logging

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device_token import DeviceToken
from app.models.models import Transaction, TransactionStatus
from app.schemas.schemas import JekoWebhookPayload
from app.services.push_service import get_push_service
from app.services.wallet_service import credit_commission, get_wallet_for_update

logger = logging.getLogger("webhook_service")

TERMINAL_STATUSES = {TransactionStatus.SUCCESS, TransactionStatus.FAILED, TransactionStatus.CANCELLED}
WEBHOOK_DEDUP_TTL_SECONDS = 300

# Mapping des statuts JEKO -> statuts internes.
# La vraie API JEKO n'utilise que deux valeurs en minuscules : "success" et
# "error" (pas de "pending"/"cancelled" côté webhook — un webhook n'est
# envoyé qu'une fois la transaction terminée, dans un sens ou l'autre).
JEKO_STATUS_MAP = {
    "success": TransactionStatus.SUCCESS,
    "error": TransactionStatus.FAILED,
}


class WebhookAlreadyProcessed(Exception):
    pass


class TransactionNotFound(Exception):
    pass


async def acquire_webhook_dedup_lock(redis: Redis, jeko_transaction_id: str) -> bool:
    """Retourne True si ce webhook n'a jamais été vu (et pose le verrou), False sinon."""
    key = f"webhook:jeko:{jeko_transaction_id}"
    return bool(await redis.set(key, "1", nx=True, ex=WEBHOOK_DEDUP_TTL_SECONDS))


async def _notify_agent_best_effort(db: AsyncSession, transaction: Transaction) -> None:
    """
    Envoie une notification push best-effort à tous les appareils de
    l'AGENT. Toute erreur est capturée et loggée : la notification ne doit
    JAMAIS faire échouer le traitement métier du webhook.
    """
    push_service = get_push_service()
    if push_service is None:
        return

    try:
        result = await db.execute(select(DeviceToken).where(DeviceToken.user_id == transaction.agent_id))
        tokens = result.scalars().all()
        if not tokens:
            return

        if transaction.status == TransactionStatus.SUCCESS:
            title = "Opération réussie"
            body = f"Transaction de {transaction.amount} XOF confirmée. Commission : +{transaction.commission_amount} XOF."
        elif transaction.status == TransactionStatus.FAILED:
            title, body = "Opération échouée", f"La transaction de {transaction.amount} XOF a échoué."
        else:
            return

        for device in tokens:
            try:
                await push_service.send(
                    device.fcm_token,
                    title,
                    body,
                    data={"internal_reference": transaction.internal_reference, "status": transaction.status.value},
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Échec envoi push à un device: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Échec notification push (best-effort, ignoré): %s", exc)


async def process_jeko_webhook(
    db: AsyncSession,
    redis: Redis,
    payload: JekoWebhookPayload,
) -> Transaction:
    """
    Applique le webhook JEKO de façon idempotente :
      - retrouve la transaction via `internal_reference`
      - si déjà dans un état terminal -> no-op (idempotent, renvoie la transaction telle quelle)
      - sinon applique la mutation de solde correspondant au type de transaction
    """
    is_new = await acquire_webhook_dedup_lock(redis, payload.data.id)
    if not is_new:
        logger.info("Webhook JEKO %s déjà en cours/traité, ignoré", payload.data.id)
        raise WebhookAlreadyProcessed(payload.data.id)

    internal_reference = payload.data.transactionDetails.reference if payload.data.transactionDetails else None
    if not internal_reference:
        # Sans référence exploitable, impossible de retrouver notre transaction.
        raise TransactionNotFound(payload.data.id)

    stmt = select(Transaction).where(Transaction.internal_reference == internal_reference)
    result = await db.execute(stmt)
    transaction = result.scalar_one_or_none()

    if transaction is None:
        raise TransactionNotFound(internal_reference)

    if transaction.status in TERMINAL_STATUSES:
        # Idempotence : webhook redondant sur une transaction déjà finalisée
        logger.info("Transaction %s déjà finalisée (%s), webhook ignoré", transaction.internal_reference, transaction.status)
        return transaction

    new_status = JEKO_STATUS_MAP.get(payload.data.status.lower(), TransactionStatus.PENDING)
    transaction.jeko_reference = payload.data.id

    if new_status == TransactionStatus.SUCCESS:
        # Peu importe le sens (Cash-In ou Cash-Out), l'agent a droit à sa
        # commission dès que JEKO confirme le succès réel de l'opération.
        wallet = await get_wallet_for_update(db, transaction.agent_id)
        await credit_commission(db, wallet, transaction.commission_amount)
        transaction.status = TransactionStatus.SUCCESS

    elif new_status == TransactionStatus.FAILED:
        # Aucun solde interne n'a été débité à l'initiation (le float réel
        # est géré par JEKO) : rien à recréditer, juste marquer l'échec.
        transaction.status = TransactionStatus.FAILED
        transaction.metadata_ = {
            **(transaction.metadata_ or {}),
            "failure_reason": payload.data.description or "Transaction refusée par l'opérateur",
        }

    else:
        transaction.status = new_status

    await db.flush()
    await _notify_agent_best_effort(db, transaction)
    return transaction
