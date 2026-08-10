"""Service métier bas niveau pour le wallet et les transactions utilisateur."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import AsyncIterator

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import MobileOperator, Transaction, TransactionStatus, TransactionType, Wallet

LOCK_TTL_SECONDS = 15


class WalletLockError(Exception):
    pass


class InsufficientBalanceError(Exception):
    pass


def generate_internal_reference(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"


@asynccontextmanager
async def user_redis_lock(redis: Redis, user_id: uuid.UUID) -> AsyncIterator[None]:
    lock_key = f"user:lock:{user_id}"
    acquired = await redis.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS)
    if not acquired:
        raise WalletLockError(f"Opération déjà en cours pour l'utilisateur {user_id}")
    try:
        yield
    finally:
        await redis.delete(lock_key)


async def get_wallet_for_update(db: AsyncSession, user_id: uuid.UUID) -> Wallet:
    stmt = select(Wallet).where(Wallet.user_id == user_id).with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one()


async def create_pending_deposit(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    internal_reference: str,
    operator: MobileOperator,
    amount: Decimal,
    source_phone: str,
) -> Transaction:
    transaction = Transaction(
        internal_reference=internal_reference,
        user_id=user_id,
        type=TransactionType.DEPOSIT,
        source_operator=operator,
        destination_operator=None,
        amount=amount,
        fee=Decimal("0"),
        total_collected=amount,
        jeko_deposit_fee_rate=Decimal("0"),
        payin_status=TransactionStatus.PENDING,
        payout_status=None,
        status=TransactionStatus.PENDING,
        recipient_name=None,
        recipient_phone=None,
        metadata_={"source_phone": source_phone},
    )
    db.add(transaction)
    await db.flush()
    return transaction


async def create_pending_transfer(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    internal_reference: str,
    source_operator: MobileOperator,
    destination_operator: MobileOperator,
    net_amount: Decimal,
    platform_fee: Decimal,
    total_collected: Decimal,
    jeko_deposit_fee_rate: Decimal,
    recipient_name: str,
    recipient_phone: str,
    source_phone: str,
) -> Transaction:
    transaction = Transaction(
        internal_reference=internal_reference,
        user_id=user_id,
        type=TransactionType.TRANSFER,
        source_operator=source_operator,
        destination_operator=destination_operator,
        amount=net_amount,
        fee=platform_fee,
        total_collected=total_collected,
        jeko_deposit_fee_rate=jeko_deposit_fee_rate,
        payin_status=TransactionStatus.PENDING,
        payout_status=None,
        status=TransactionStatus.PENDING,
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
        metadata_={"source_phone": source_phone},
    )
    db.add(transaction)
    await db.flush()
    return transaction


async def create_pending_withdrawal(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    internal_reference: str,
    operator: MobileOperator,
    amount: Decimal,
) -> Transaction:
    transaction = Transaction(
        internal_reference=internal_reference,
        user_id=user_id,
        type=TransactionType.WITHDRAWAL,
        source_operator=None,
        destination_operator=operator,
        amount=amount,
        fee=Decimal("0"),
        total_collected=None,
        jeko_deposit_fee_rate=None,
        payin_status=TransactionStatus.SUCCESS,
        payout_status=TransactionStatus.PENDING,
        status=TransactionStatus.PENDING,
        recipient_name=None,
        recipient_phone=None,
    )
    db.add(transaction)
    await db.flush()
    return transaction


async def debit_wallet(db: AsyncSession, wallet: Wallet, amount: Decimal) -> None:
    if Decimal(wallet.balance) < amount:
        raise InsufficientBalanceError(f"Solde insuffisant : {wallet.balance} XOF disponible, {amount} XOF demandé.")
    wallet.balance = Decimal(wallet.balance) - amount


async def credit_wallet(db: AsyncSession, wallet: Wallet, amount: Decimal) -> None:
    wallet.balance = Decimal(wallet.balance) + amount
