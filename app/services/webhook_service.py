"""
Traitement des webhooks JEKO (`transaction.completed`) — Ayak'bine v2.

Contrairement à un simple Cash-In/Cash-Out à une étape, un TRANSFER v2 est
une opération en DEUX étapes chaînées, chacune confirmée par SON PROPRE
webhook (références dérivées `{internal_reference}-IN` puis `-OUT`) :

  1. Webhook sur la référence "...-IN" (pay-in, collecte chez le client) :
       - succès  -> on déclenche IMMÉDIATEMENT le pay-out vers le destinataire
       - échec   -> rien n'a été collecté, transfert FAILED, aucun impact wallet

  2. Webhook sur la référence "...-OUT" (pay-out, versement au destinataire) :
       - succès  -> transfert (ou retrait) SUCCESS
       - échec   -> le montant collecté (moins la commission JEKO déjà
                    déduite au pay-in) est recrédité sur le wallet interne
                    du client (filet de sécurité) ; statut FAILED_PAYOUT
                    pour un TRANSFER, FAILED pour un WITHDRAWAL simple
                    (recrédit intégral, il n'y a pas eu de pay-in séparé).

Un WITHDRAWAL n'a qu'une étape ("...-OUT" uniquement, payin_status déjà
SUCCESS dès la création — voir wallet_service.create_pending_withdrawal).
"""
from __future__ import annotations

import logging
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Transaction, TransactionStatus, TransactionType
from app.schemas.schemas import JekoWebhookPayload
from app.services.fee_rules import compute_wallet_credit_on_payout_failure
from app.services.jeko_client import JekoAPIError, JekoClient, JekoNetworkError
from app.services.wallet_service import credit_wallet, get_wallet_for_update

logger = logging.getLogger("webhook_service")

TERMINAL_LEG_STATUSES = {TransactionStatus.SUCCESS, TransactionStatus.FAILED}


class WebhookAlreadyProcessed(Exception):
    """Ce webhook JEKO (par son id) a déjà été traité — à acquitter sans le retraiter."""


class TransactionNotFound(Exception):
    """Aucune transaction interne ne correspond à la référence reçue."""


def _split_reference(reference: str) -> tuple[str, str]:
    """
    "TRF-ABCD1234-IN" -> ("TRF-ABCD1234", "IN")
    "WDR-ABCD1234-OUT" -> ("WDR-ABCD1234", "OUT")
    """
    if reference.endswith("-IN"):
        return reference[: -len("-IN")], "IN"
    if reference.endswith("-OUT"):
        return reference[: -len("-OUT")], "OUT"
    # Référence inattendue (ancien format, ou appel manuel de test) : on la
    # traite telle quelle comme un "OUT" par défaut plutôt que de planter,
    # mais ce cas ne devrait normalement jamais se produire en usage normal.
    logger.warning("Référence webhook sans suffixe -IN/-OUT reconnu: %s", reference)
    return reference, "OUT"


async def acquire_webhook_dedup_lock(redis: Redis, jeko_event_id: str) -> bool:
    """SETNX Redis : True si c'est la première fois qu'on voit cet id JEKO."""
    key = f"webhook:jeko:{jeko_event_id}"
    return bool(await redis.set(key, "1", nx=True, ex=60 * 60 * 24))


async def process_jeko_webhook(
    db: AsyncSession,
    redis: Redis,
    payload: JekoWebhookPayload,
    jeko: JekoClient,
) -> Transaction:
    data = payload.data

    is_new = await acquire_webhook_dedup_lock(redis, data.id)
    if not is_new:
        logger.info("Webhook JEKO %s déjà en cours/traité, ignoré", data.id)
        raise WebhookAlreadyProcessed(data.id)

    if data.transactionDetails is None or not data.transactionDetails.reference:
        raise TransactionNotFound("(reference manquante dans transactionDetails)")

    base_reference, leg = _split_reference(data.transactionDetails.reference)

    stmt = select(Transaction).where(Transaction.internal_reference == base_reference)
    result = await db.execute(stmt)
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise TransactionNotFound(base_reference)

    is_success = data.status.lower() == "success"
    new_leg_status = TransactionStatus.SUCCESS if is_success else TransactionStatus.FAILED

    if leg == "IN":
        await _handle_payin_webhook(db, transaction, new_leg_status, jeko_event_id=data.id, jeko=jeko)
    else:
        await _handle_payout_webhook(db, transaction, new_leg_status)

    return transaction


async def _handle_payin_webhook(
    db: AsyncSession,
    transaction: Transaction,
    new_status: TransactionStatus,
    *,
    jeko_event_id: str,
    jeko: JekoClient,
) -> None:
    if transaction.payin_status in TERMINAL_LEG_STATUSES:
        logger.info("Pay-in déjà finalisé pour %s (%s), webhook ignoré", transaction.internal_reference, transaction.payin_status)
        return

    transaction.payin_status = new_status

    if new_status == TransactionStatus.FAILED:
        # Rien n'a été collecté : le transfert entier échoue, aucun impact wallet.
        transaction.status = TransactionStatus.FAILED
        return

    # Pay-in réussi : on déclenche IMMÉDIATEMENT le pay-out vers le destinataire.
    try:
        jeko_response = await jeko.create_withdrawal_transfer(
            internal_reference=f"{transaction.internal_reference}-OUT",
            amount=Decimal(transaction.amount),
            operator=transaction.destination_operator.value,
            phone_number=transaction.recipient_phone,
            beneficiary_name=transaction.recipient_name or "Bénéficiaire",
        )
        transaction.jeko_payout_id = jeko_response.get("id")
        transaction.payout_status = TransactionStatus.PENDING
        # status global reste PENDING jusqu'au webhook du pay-out
    except (JekoAPIError, JekoNetworkError) as exc:
        # Le pay-in a réussi mais on n'a même pas pu INITIER le pay-out :
        # aucun webhook de pay-out ne viendra jamais pour cette tentative.
        # On recrédite tout de suite plutôt que de laisser l'argent bloqué
        # en attente d'un événement qui n'arrivera pas.
        logger.error(
            "Échec d'initiation du pay-out après pay-in réussi (%s): %s",
            transaction.internal_reference, exc,
        )
        wallet = await get_wallet_for_update(db, transaction.user_id)
        credit_amount = compute_wallet_credit_on_payout_failure(
            Decimal(transaction.total_collected), transaction.source_operator
        )
        await credit_wallet(db, wallet, credit_amount)
        transaction.payout_status = TransactionStatus.FAILED
        transaction.status = TransactionStatus.FAILED_PAYOUT
        transaction.metadata_ = {
            **(transaction.metadata_ or {}),
            "payout_initiation_error": str(exc),
        }



async def _handle_payout_webhook(
    db: AsyncSession,
    transaction: Transaction,
    new_status: TransactionStatus,
) -> None:
    if transaction.payout_status in TERMINAL_LEG_STATUSES:
        logger.info("Pay-out déjà finalisé pour %s (%s), webhook ignoré", transaction.internal_reference, transaction.payout_status)
        return

    transaction.payout_status = new_status

    if new_status == TransactionStatus.SUCCESS:
        transaction.status = TransactionStatus.SUCCESS
        return

    # Pay-out échoué : on recrédite le wallet.
    wallet = await get_wallet_for_update(db, transaction.user_id)

    if transaction.type == TransactionType.TRANSFER:
        # Le pay-in avait réussi (sinon on ne serait jamais arrivé à l'étape
        # pay-out) : on recrédite le total collecté, net de la commission
        # JEKO déjà déduite silencieusement à la collecte.
        credit_amount = compute_wallet_credit_on_payout_failure(
            Decimal(transaction.total_collected), transaction.source_operator
        )
        transaction.status = TransactionStatus.FAILED_PAYOUT
    else:
        # WITHDRAWAL simple : le montant avait été débité intégralement du
        # wallet à l'initiation (voir wallet.py) ; aucun frais plateforme
        # sur un retrait (fee=0), donc recrédit intégral du montant débité.
        credit_amount = Decimal(transaction.amount)
        transaction.status = TransactionStatus.FAILED

    await credit_wallet(db, wallet, credit_amount)
