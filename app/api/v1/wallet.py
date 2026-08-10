"""
Endpoints du wallet interne : consultation du solde et retrait vers Mobile
Money. Le wallet sert de filet de sécurité (crédité automatiquement si un
pay-out de transfert échoue après collecte réussie — voir webhook_service.py)
et reste librement retirable par le client à tout moment.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, verify_pin
from app.database import get_db, get_redis
from app.models.models import TransactionStatus, User
from app.schemas.schemas import WalletBalanceResponse, WithdrawRequest, WithdrawResponse
from app.services.auto_reconcile import schedule_auto_reconcile
from app.services.jeko_client import JekoAPIError, JekoClient, JekoNetworkError, get_jeko_client
from app.services.wallet_service import (
    InsufficientBalanceError,
    WalletLockError,
    create_pending_withdrawal,
    debit_wallet,
    generate_internal_reference,
    get_wallet_for_update,
    user_redis_lock,
)

logger = logging.getLogger("wallet")

router = APIRouter(prefix="/api/v1/wallet", tags=["Wallet"])


@router.get("/me", response_model=WalletBalanceResponse)
async def get_my_wallet(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    wallet = await get_wallet_for_update(db, current_user.id)
    return WalletBalanceResponse(balance=wallet.balance, currency=wallet.currency)


@router.post("/withdraw", response_model=WithdrawResponse)
async def withdraw(
    payload: WithdrawRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    jeko: JekoClient = Depends(get_jeko_client),
):
    if not verify_pin(payload.pin_code, current_user.pin_code_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Code PIN incorrect")

    internal_reference = generate_internal_reference("WDR")

    try:
        async with user_redis_lock(redis, current_user.id):
            wallet = await get_wallet_for_update(db, current_user.id)
            try:
                await debit_wallet(db, wallet, payload.amount)
            except InsufficientBalanceError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

            transaction = await create_pending_withdrawal(
                db,
                user_id=current_user.id,
                internal_reference=internal_reference,
                operator=payload.operator,
                amount=payload.amount,
            )

            try:
                jeko_response = await jeko.create_withdrawal_transfer(
                    internal_reference=f"{internal_reference}-OUT",
                    amount=payload.amount,
                    operator=payload.operator.value,
                    phone_number=current_user.phone_number,
                    beneficiary_name=current_user.full_name,
                )
            except (JekoAPIError, JekoNetworkError) as exc:
                # Échec immédiat à l'initiation (pas juste "en attente") : on
                # recrédite tout de suite plutôt que d'attendre un webhook
                # qui ne viendra jamais pour un appel qui n'a jamais abouti.
                wallet.balance = wallet.balance + payload.amount
                transaction.payout_status = transaction.status = TransactionStatus.FAILED
                await db.commit()
                detail = (
                    f"Échec de l'initiation du retrait côté JEKO: {exc.payload}"
                    if isinstance(exc, JekoAPIError)
                    else "Service de paiement momentanément indisponible, réessayez."
                )
                status_code = status.HTTP_400_BAD_REQUEST if isinstance(exc, JekoAPIError) else status.HTTP_502_BAD_GATEWAY
                raise HTTPException(status_code=status_code, detail=detail) from exc

            transaction.jeko_payout_id = jeko_response.get("id")
            await db.commit()
    except WalletLockError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc

    # Filet de sécurité automatique (voir app/services/auto_reconcile.py).
    schedule_auto_reconcile(internal_reference)

    return WithdrawResponse(
        internal_reference=internal_reference,
        status=transaction.status,
        message="Retrait initié, vous recevrez une confirmation une fois traité.",
    )
