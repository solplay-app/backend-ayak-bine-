from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, verify_pin
from app.database import get_db, get_redis
from app.models.models import Transaction, TransactionStatus, User
from app.schemas.schemas import (
    DepositConfirmRequest,
    DepositConfirmResponse,
    DepositRequest,
    DepositResponse,
    TransactionRead,
    WalletBalanceResponse,
    WithdrawRequest,
    WithdrawResponse,
)
from app.config import get_settings
from app.services import kkiapay_client
from app.services.auto_reconcile import schedule_auto_reconcile
from app.services.jeko_client import JekoAPIError, JekoClient, JekoNetworkError, get_jeko_client
from app.services.wallet_service import (
    InsufficientBalanceError,
    WalletLockError,
    create_pending_deposit,
    create_pending_withdrawal,
    credit_wallet,
    debit_wallet,
    finalize_kkiapay_deposit,
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
):
    """
    Recharge du wallet — temporairement sur Kkiapay Sandbox (JEKO en attente
    de validation KYC). Contrairement à JEKO, c'est l'app Flutter qui ouvre
    le widget de paiement Kkiapay avec les infos retournées ici ; le backend
    ne fait que créer la transaction PENDING. La confirmation réelle se fait
    ensuite via POST /deposit/confirm, vérifiée server-side auprès de Kkiapay
    (jamais sur la seule foi du callback client, falsifiable).
    """
    if not verify_pin(payload.pin_code, current_user.pin_code_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Code PIN incorrect")

    settings = get_settings()
    if not settings.kkiapay_public_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recharge momentanément indisponible (Kkiapay non configuré côté serveur).",
        )

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
            await db.commit()
    except WalletLockError as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc

    return DepositResponse(
        internal_reference=internal_reference,
        status=transaction.status,
        amount=payload.amount,
        message="Ouvrez le paiement Kkiapay pour confirmer la recharge.",
        kkiapay_public_key=settings.kkiapay_public_key,
        kkiapay_sandbox=settings.kkiapay_sandbox,
    )


@router.post("/deposit/confirm", response_model=DepositConfirmResponse)
async def confirm_deposit(
    payload: DepositConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Appelée par l'app juste après la fermeture du widget Kkiapay (succès OU
    échec côté client). On ne fait JAMAIS confiance au callback de l'app —
    on revérifie toujours le vrai statut auprès de Kkiapay avant de créditer
    quoi que ce soit. Idempotent (voir finalize_kkiapay_deposit).
    """
    result = await db.execute(
        select(Transaction).where(
            Transaction.internal_reference == payload.internal_reference,
            Transaction.user_id == current_user.id,
        )
    )
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction introuvable")

    if transaction.payin_status != TransactionStatus.PENDING:
        return DepositConfirmResponse(
            internal_reference=transaction.internal_reference,
            status=transaction.status,
            message="Cette recharge a déjà été traitée.",
        )

    try:
        kkiapay_data = await kkiapay_client.verify_transaction(payload.kkiapay_transaction_id)
    except kkiapay_client.KkiapayNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - erreur réseau/HTTP vers Kkiapay
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Impossible de vérifier le paiement auprès de Kkiapay pour le moment, réessayez.",
        ) from exc

    success = kkiapay_client.is_success(kkiapay_data)
    await finalize_kkiapay_deposit(
        db, transaction, success=success, kkiapay_transaction_id=payload.kkiapay_transaction_id
    )
    await db.commit()

    return DepositConfirmResponse(
        internal_reference=transaction.internal_reference,
        status=transaction.status,
        message="Recharge confirmée !" if success else "Le paiement Kkiapay n'a pas abouti.",
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
