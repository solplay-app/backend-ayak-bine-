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
from app.services.push_service import get_push_service
from app.services.wallet_service import credit_wallet, get_wallet_for_update

logger = logging.getLogger("webhook_service")

TERMINAL_LEG_STATUSES = {TransactionStatus.SUCCESS, TransactionStatus.FAILED}


async def _notify(db: AsyncSession, transaction: Transaction, *, title: str, body: str) -> None:
    push_service = get_push_service()
    if push_service is None:
        return
    await push_service.notify_user(
        db,
        transaction.user_id,
        title=title,
        body=body,
        data={"internal_reference": transaction.internal_reference, "type": "transaction_update"},
    )


class WebhookAlreadyProcessed(Exception):
    pass


class TransactionNotFound(Exception):
    pass


def _split_reference(reference: str) -> tuple[str, str]:
    if reference.endswith("-IN"):
        return reference[: -len("-IN")], "IN"
    if reference.endswith("-OUT"):
        return reference[: -len("-OUT")], "OUT"
    logger.warning("Référence webhook sans suffixe -IN/-OUT reconnu: %s", reference)
    return reference, "OUT"


async def acquire_webhook_dedup_lock(redis: Redis, jeko_event_id: str) -> bool:
    return bool(await redis.set(f"webhook:jeko:{jeko_event_id}", "1", nx=True, ex=60 * 60 * 24))


async def process_jeko_webhook(
    db: AsyncSession,
    redis: Redis,
    payload: JekoWebhookPayload,
    jeko: JekoClient,
) -> Transaction:
    data = payload.data
    is_new = await acquire_webhook_dedup_lock(redis, data.id)
    if not is_new:
        raise WebhookAlreadyProcessed(data.id)

    if data.transactionDetails is None or not data.transactionDetails.reference:
        raise TransactionNotFound("reference manquante")

    base_reference, leg = _split_reference(data.transactionDetails.reference)
    result = await db.execute(select(Transaction).where(Transaction.internal_reference == base_reference))
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise TransactionNotFound(base_reference)

    new_leg_status = TransactionStatus.SUCCESS if data.status.lower() == "success" else TransactionStatus.FAILED
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
        return

    transaction.payin_status = new_status

    if new_status == TransactionStatus.FAILED:
        transaction.status = TransactionStatus.FAILED
        if transaction.type == TransactionType.DEPOSIT:
            await _notify(db, transaction, title="Recharge échouée", body=f"Votre recharge de {transaction.amount} XOF n'a pas abouti.")
        else:
            await _notify(db, transaction, title="Transfert échoué", body=f"Le paiement de {transaction.total_collected} XOF n'a pas abouti. Aucun montant n'a été débité.")
        return

    if transaction.type == TransactionType.DEPOSIT:
        wallet = await get_wallet_for_update(db, transaction.user_id)
        await credit_wallet(db, wallet, Decimal(transaction.amount))
        transaction.status = TransactionStatus.SUCCESS
        await _notify(db, transaction, title="Wallet rechargé", body=f"{transaction.amount} XOF ont été ajoutés à votre solde Ayak'bine.")
        return

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
    except (JekoAPIError, JekoNetworkError) as exc:
        wallet = await get_wallet_for_update(db, transaction.user_id)
        credit_amount = compute_wallet_credit_on_payout_failure(Decimal(transaction.total_collected), transaction.source_operator)
        await credit_wallet(db, wallet, credit_amount)
        transaction.payout_status = TransactionStatus.FAILED
        transaction.status = TransactionStatus.FAILED_PAYOUT
        transaction.metadata_ = {**(transaction.metadata_ or {}), "payout_initiation_error": str(exc), "jeko_event_id": jeko_event_id}
        await _notify(db, transaction, title="Montant recrédité sur votre solde", body=f"Le versement à {transaction.recipient_name or 'votre destinataire'} n'a pas pu être initié. {credit_amount} XOF ont été recrédités sur votre solde Ayak'bine.")


async def _handle_payout_webhook(db: AsyncSession, transaction: Transaction, new_status: TransactionStatus) -> None:
    if transaction.payout_status in TERMINAL_LEG_STATUSES:
        return

    transaction.payout_status = new_status

    if new_status == TransactionStatus.SUCCESS:
        transaction.status = TransactionStatus.SUCCESS
        if transaction.type == TransactionType.WITHDRAWAL:
            await _notify(db, transaction, title="Retrait réussi", body=f"Votre retrait de {transaction.amount} XOF a été envoyé avec succès.")
        else:
            await _notify(db, transaction, title="Transfert réussi", body=f"{transaction.amount} XOF ont bien été envoyés à {transaction.recipient_name or 'votre destinataire'}.")
        return

    wallet = await get_wallet_for_update(db, transaction.user_id)
    if transaction.type == TransactionType.TRANSFER:
        credit_amount = compute_wallet_credit_on_payout_failure(Decimal(transaction.total_collected), transaction.source_operator)
        transaction.status = TransactionStatus.FAILED_PAYOUT
        notif_title = "Montant recrédité sur votre solde"
        notif_body = f"Le versement à {transaction.recipient_name or 'votre destinataire'} a échoué. {credit_amount} XOF ont été recrédités sur votre solde Ayak'bine."
    else:
        credit_amount = Decimal(transaction.amount)
        transaction.status = TransactionStatus.FAILED
        notif_title = "Retrait échoué"
        notif_body = f"Votre retrait de {credit_amount} XOF a échoué et a été recrédité sur votre solde."

    await credit_wallet(db, wallet, credit_amount)
    await _notify(db, transaction, title=notif_title, body=notif_body)
