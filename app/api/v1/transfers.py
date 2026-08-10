"""
Endpoints du TRANSFERT inter-opérateurs Mobile Money (v2) — remplace
l'ancien modèle Cash-In/Cash-Out agent.

Flow : le client paie un montant NET + 8% de frais plateforme depuis son
opérateur source (pay-in JEKO) ; dès confirmation par webhook, le backend
déclenche automatiquement le versement du montant net au destinataire sur
l'opérateur choisi (pay-out JEKO). Voir services/webhook_service.py pour
l'enchaînement pay-in -> pay-out et la gestion du filet de sécurité wallet.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, verify_pin
from app.database import get_db, get_redis
from app.models.models import Transaction, TransactionStatus, TransactionType, User
from app.schemas.schemas import TransferDetailResponse, TransferRequest, TransferResponse
from app.services.fee_rules import JEKO_DEPOSIT_FEE_RATE, compute_platform_fee, compute_total_to_collect
from app.services.auto_reconcile import schedule_auto_reconcile
from app.services.jeko_client import JekoAPIError, JekoClient, JekoNetworkError, get_jeko_client
from app.services.wallet_service import (
    WalletLockError,
    create_pending_transfer,
    generate_internal_reference,
    user_redis_lock,
)

logger = logging.getLogger("transfers")

router = APIRouter(prefix="/api/v1/transfers", tags=["Transferts"])


@router.post("", response_model=TransferResponse)
async def create_transfer(
    payload: TransferRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    jeko: JekoClient = Depends(get_jeko_client),
):
    if not verify_pin(payload.pin_code, current_user.pin_code_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Code PIN incorrect")

    if payload.recipient_phone == current_user.phone_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Le destinataire ne peut pas être votre propre numéro.",
        )

    platform_fee = compute_platform_fee(payload.amount)
    total_to_collect = compute_total_to_collect(payload.amount)
    deposit_fee_rate = JEKO_DEPOSIT_FEE_RATE[payload.source_operator]

    internal_reference = generate_internal_reference("TRF")

    try:
        async with user_redis_lock(redis, current_user.id):
            transaction = await create_pending_transfer(
                db,
                user_id=current_user.id,
                internal_reference=internal_reference,
                source_operator=payload.source_operator,
                destination_operator=payload.destination_operator,
                net_amount=payload.amount,
                platform_fee=platform_fee,
                total_collected=total_to_collect,
                jeko_deposit_fee_rate=deposit_fee_rate,
                recipient_name=payload.recipient_name,
                recipient_phone=payload.recipient_phone,
            )

            try:
                jeko_response = await jeko.create_deposit_payment_request(
                    internal_reference=f"{internal_reference}-IN",
                    amount=total_to_collect,
                    operator=payload.source_operator.value,
                    phone_number=current_user.phone_number,
                )
            except JekoAPIError as exc:
                transaction.payin_status = transaction.status = TransactionStatus.FAILED
                await db.commit()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Échec de l'initiation du paiement côté JEKO: {exc.payload}",
                ) from exc
            except JekoNetworkError as exc:
                # La transaction reste PENDING : le client peut réessayer,
                # ou un job de réconciliation la vérifiera plus tard.
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Service de paiement momentanément indisponible, réessayez.",
                ) from exc

            transaction.jeko_payin_id = jeko_response.get("id")
            await db.commit()
    except WalletLockError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc

    # Filet de sécurité : si le webhook JEKO ne nous parvient jamais (ex. app
    # endormie sur le plan gratuit Render), on vérifie nous-mêmes le vrai
    # statut après quelques minutes plutôt que de laisser le client bloqué
    # sur "En cours" indéfiniment. Sans effet si le vrai webhook arrive avant.
    schedule_auto_reconcile(internal_reference)

    return TransferResponse(
        internal_reference=internal_reference,
        status=transaction.status,
        net_amount=payload.amount,
        platform_fee=platform_fee,
        total_to_pay=total_to_collect,
        redirect_url=jeko_response.get("redirectUrl"),
        message="Transfert initié. Confirmez le paiement sur votre téléphone.",
    )


@router.get("/{internal_reference}", response_model=TransferDetailResponse)
async def get_transfer(
    internal_reference: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Statut détaillé (payin_status + payout_status), pour le polling de l'écran 'En cours'."""
    stmt = select(Transaction).where(
        Transaction.internal_reference == internal_reference,
        Transaction.user_id == current_user.id,
        Transaction.type == TransactionType.TRANSFER,
    )
    result = await db.execute(stmt)
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfert introuvable")
    return transaction


@router.get("", response_model=list[TransferDetailResponse])
async def list_transfers(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
):
    """Historique des transferts du client, du plus récent au plus ancien."""
    stmt = (
        select(Transaction)
        .where(Transaction.user_id == current_user.id, Transaction.type == TransactionType.TRANSFER)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return result.scalars().all()
