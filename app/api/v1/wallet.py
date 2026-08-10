from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, verify_pin
from app.database import get_db, get_redis
from app.models.models import Transaction, TransactionStatus, User
from app.schemas.schemas import DepositRequest, DepositResponse, TransactionRead, WalletBalanceResponse, WithdrawRequest, WithdrawResponse
from app.services.auto_reconcile import schedule_auto_reconcile
from app.services.jeko_client import JekoAPIError, JekoClient, JekoNetworkError, get_jeko_client
from app.services.wallet_service import (
    InsufficientBalanceError,
    WalletLockError,
    create_pending_deposit,
    create_pending_withdrawal,
    credit_wallet,
    debit_wallet,
    generate_internal_reference,
    get_wallet_for_update,
    user_redis_lock,
)

logger = logging.getLogger("wallet")
router = APIRouter(prefix="/api/v1/wallet", tags=["Wallet"])


@router.get("/me", response_model=WalletBalanceResponse)
async def get_my_wallet(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    wallet = await get_wallet_for_update(db, current_user.id)
    return WalletBalanceResponse(balance=wallet.balance, currency=wallet.currency)


@router.get("/transactions", response_model=list[TransactionRead])
async def list_my_transactions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
):
    stmt = select(Transaction).where(Transaction.user_id == current_user.id).order_by(Transaction.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/deposit", response_model=DepositResponse)
async def deposit(
    payload: DepositRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    jeko: JekoClient = Depends(get_jeko_client),
):
    if not verify_pin(payload.pin_code, current_user.pin_code_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Code PIN incorrect")

    source_phone = payload.source_phone or current_user.phone_number
    internal_reference = generate_internal_reference("DEP")

    try:
        async with user_redis_lock(redis, current_user.id):
            transaction = await create_pending_deposit(
                db,
                user_id=current_user.id,
                internal_reference=internal_reference,
                operator=payload.operator,
                amount=payload.amount,
                source_phone=source_phone,
            )
            try:
                jeko_response = await jeko.create_deposit_payment_request(
                    internal_reference=f"{internal_reference}-IN",
                    amount=payload.amount,
                    operator=payload.operator.value,
                    phone_number=source_phone,
                )
            except JekoAPIError as exc:
                transaction.payin_status = transaction.status = TransactionStatus.FAILED
                await db.commit()
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Échec de l'initiation de la recharge côté JEKO: {exc.payload}") from exc
            except JekoNetworkError as exc:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Service de paiement momentanément indisponible, réessayez.") from exc

            transaction.jeko_payin_id = jeko_response.get("id")
            await db.commit()
    except WalletLockError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc

    schedule_auto_reconcile(internal_reference)
    return DepositResponse(
        internal_reference=internal_reference,
        status=transaction.status,
        redirect_url=jeko_response.get("redirectUrl"),
        message="Recharge initiée. Confirmez le paiement sur votre téléphone.",
    )


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

            transaction = await create_pending_withdrawal(db, user_id=current_user.id, internal_reference=internal_reference, operator=payload.operator, amount=payload.amount)
            try:
                jeko_response = await jeko.create_withdrawal_transfer(
                    internal_reference=f"{internal_reference}-OUT",
                    amount=payload.amount,
                    operator=payload.operator.value,
                    phone_number=current_user.phone_number,
                    beneficiary_name=current_user.full_name,
                )
            except (JekoAPIError, JekoNetworkError) as exc:
                await credit_wallet(db, wallet, payload.amount)
                transaction.payout_status = transaction.status = TransactionStatus.FAILED
                await db.commit()
                detail = f"Échec de l'initiation du retrait côté JEKO: {exc.payload}" if isinstance(exc, JekoAPIError) else "Service de paiement momentanément indisponible, réessayez."
                status_code = status.HTTP_400_BAD_REQUEST if isinstance(exc, JekoAPIError) else status.HTTP_502_BAD_GATEWAY
                raise HTTPException(status_code=status_code, detail=detail) from exc

            transaction.jeko_payout_id = jeko_response.get("id")
            await db.commit()
    except WalletLockError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc

    schedule_auto_reconcile(internal_reference)
    return WithdrawResponse(internal_reference=internal_reference, status=transaction.status, message="Retrait initié, vous recevrez une confirmation une fois traité.")
