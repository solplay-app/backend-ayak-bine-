from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, verify_pin
from app.database import get_db, get_redis
from app.models.models import MobileOperator, Transaction, TransactionStatus, TransactionType, User, Wallet
from app.schemas.schemas import (
    DepositRequest,
    DepositResponse,
    TransactionRead,
    WalletBalanceResponse,
    WithdrawRequest,
    WithdrawResponse,
)
from app.services.jeko_client import JekoAPIError, JekoClient, JekoNetworkError, get_jeko_client
from app.services.wallet_service import (
    InsufficientBalanceError,
    WalletLockError,
    create_pending_transaction,
    debit_wallet,
    generate_internal_reference,
    get_wallet_for_update,
    wallet_redis_lock,
)
from sqlalchemy import select

router = APIRouter(prefix="/api/v1/wallet", tags=["Wallet"])

WITHDRAWAL_FEE_RATE = Decimal("0.01")  # 1% (exemple, à ajuster selon la grille tarifaire réelle)


@router.get("/me", response_model=WalletBalanceResponse)
async def get_my_wallet(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Solde courant du Wallet applicatif de l'utilisateur connecté."""
    result = await db.execute(select(Wallet).where(Wallet.user_id == current_user.id))
    wallet = result.scalar_one_or_none()
    if wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wallet introuvable")
    return WalletBalanceResponse(balance=wallet.balance, currency=wallet.currency)


@router.post("/deposit", response_model=DepositResponse, status_code=status.HTTP_201_CREATED)
async def deposit(
    payload: DepositRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    jeko: JekoClient = Depends(get_jeko_client),
):
    """
    Cash-In : initie un dépôt Mobile Money -> Wallet applicatif.
    La transaction reste PENDING jusqu'à réception du webhook JEKO validé.
    """
    if not verify_pin(payload.pin_code, current_user.pin_code_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Code PIN incorrect")

    internal_reference = generate_internal_reference("DEP")

    transaction = await create_pending_transaction(
        db,
        user_id=current_user.id,
        internal_reference=internal_reference,
        tx_type=TransactionType.DEPOSIT,
        operator=payload.operator,
        amount=payload.amount,
        metadata={"phone_number": payload.phone_number},
    )
    await db.commit()

    try:
        jeko_response = await jeko.initiate_payin(
            internal_reference=internal_reference,
            amount=payload.amount,
            operator=payload.operator.value,
            phone_number=payload.phone_number,
        )
    except JekoAPIError as exc:
        transaction.status = TransactionStatus.FAILED
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Échec de l'initiation du dépôt côté JEKO: {exc.payload}",
        ) from exc
    except JekoNetworkError as exc:
        # La transaction reste PENDING : un job de réconciliation pourra la vérifier plus tard
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Service de paiement momentanément indisponible, réessayez.",
        ) from exc

    transaction.jeko_reference = jeko_response.get("transaction_id")
    await db.commit()

    return DepositResponse(
        internal_reference=internal_reference,
        status=TransactionStatus.PENDING,
        payment_link=jeko_response.get("payment_link"),
        message="Dépôt initié. Confirmez l'opération sur votre téléphone.",
    )


@router.post("/withdraw", response_model=WithdrawResponse, status_code=status.HTTP_201_CREATED)
async def withdraw(
    payload: WithdrawRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    jeko: JekoClient = Depends(get_jeko_client),
    redis: Redis = Depends(get_redis),
):
    """
    Cash-Out : décaissement du Wallet applicatif vers le Mobile Money du client.
    Le débit est optimiste (effectué avant confirmation JEKO) et protégé par :
      - un verrou distribué Redis (anti double-soumission concurrente)
      - un verrou pessimiste PostgreSQL (SELECT ... FOR UPDATE)
      - un recrédit automatique si le webhook renvoie FAILED
    """
    if not verify_pin(payload.pin_code, current_user.pin_code_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Code PIN incorrect")

    fee = (payload.amount * WITHDRAWAL_FEE_RATE).quantize(Decimal("0.01"))
    internal_reference = generate_internal_reference("WDR")

    try:
        async with wallet_redis_lock(redis, current_user.id):
            wallet = await get_wallet_for_update(db, current_user.id)

            try:
                await debit_wallet(db, wallet, payload.amount, fee)
            except InsufficientBalanceError as exc:
                await db.rollback()
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

            transaction = await create_pending_transaction(
                db,
                user_id=current_user.id,
                internal_reference=internal_reference,
                tx_type=TransactionType.WITHDRAWAL,
                operator=payload.operator,
                amount=payload.amount,
                fee=fee,
                recipient_phone=payload.phone_number,
            )
            await db.commit()
    except WalletLockError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Une autre opération est déjà en cours sur votre wallet, veuillez patienter.",
        ) from exc

    try:
        jeko_response = await jeko.initiate_payout(
            internal_reference=internal_reference,
            amount=payload.amount,
            operator=payload.operator.value,
            phone_number=payload.phone_number,
        )
    except (JekoAPIError, JekoNetworkError) as exc:
        # Échec d'initiation : on recrédite immédiatement le wallet et on clôture la transaction
        async with wallet_redis_lock(redis, current_user.id):
            wallet = await get_wallet_for_update(db, current_user.id)
            wallet.balance += payload.amount + fee
            transaction.status = TransactionStatus.FAILED
            await db.commit()

        detail = (
            f"Échec du retrait côté JEKO: {exc.payload}"
            if isinstance(exc, JekoAPIError)
            else "Service de paiement momentanément indisponible, votre solde a été recrédité."
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc

    transaction.jeko_reference = jeko_response.get("transaction_id")
    await db.commit()

    return WithdrawResponse(
        internal_reference=internal_reference,
        status=TransactionStatus.PENDING,
        message="Retrait initié, en attente de confirmation JEKO.",
    )


@router.get("/transactions/{internal_reference}", response_model=TransactionRead)
async def get_transaction_status(
    internal_reference: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Consultation du statut d'une transaction (utilisé par le client mobile
    après retour du deep-link Mobile Money, ou pour un polling léger tant
    que le webhook n'a pas encore été reçu).
    """
    stmt = select(Transaction).where(
        Transaction.internal_reference == internal_reference,
        Transaction.user_id == current_user.id,
    )
    result = await db.execute(stmt)
    transaction = result.scalar_one_or_none()
    if transaction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction introuvable")
    return transaction
