"""
Service métier du Wallet.
Responsabilités :
  - Génération de références internes idempotentes
  - Locking distribué (Redis) pour éviter les race conditions sur le solde
  - Débit/crédit atomique du solde en base (verrou pessimiste SELECT ... FOR UPDATE)
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import AsyncIterator

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Transaction, TransactionStatus, Wallet

LOCK_TTL_SECONDS = 15


class InsufficientBalanceError(Exception):
    pass


class WalletLockError(Exception):
    """Une opération concurrente est déjà en cours sur ce wallet."""


def generate_internal_reference(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16].upper()}"


@asynccontextmanager
async def wallet_redis_lock(redis: Redis, user_id: uuid.UUID) -> AsyncIterator[None]:
    """
    Verrou distribué Redis (SETNX) empêchant deux opérations simultanées
    (ex: deux retraits en parallèle) sur le même wallet.
    """
    lock_key = f"wallet:lock:{user_id}"
    acquired = await redis.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS)
    if not acquired:
        raise WalletLockError(f"Opération déjà en cours pour le wallet {user_id}")
    try:
        yield
    finally:
        await redis.delete(lock_key)


async def get_wallet_for_update(db: AsyncSession, user_id: uuid.UUID) -> Wallet:
    """Verrou pessimiste PostgreSQL en complément du verrou Redis (défense en profondeur)."""
    stmt = select(Wallet).where(Wallet.user_id == user_id).with_for_update()
    result = await db.execute(stmt)
    wallet = result.scalar_one()
    return wallet


async def create_pending_transaction(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    internal_reference: str,
    tx_type,
    operator,
    amount: Decimal,
    fee: Decimal = Decimal("0"),
    recipient_phone: str | None = None,
    metadata: dict | None = None,
) -> Transaction:
    """
    Crée la transaction en base avec status=PENDING.
    `internal_reference` est UNIQUE en base : toute tentative de recréation
    avec la même référence lève une IntegrityError, ce qui garantit l'idempotence
    au niveau création (protection contre le double-clic / double-soumission).
    """
    transaction = Transaction(
        internal_reference=internal_reference,
        user_id=user_id,
        type=tx_type,
        operator=operator,
        amount=amount,
        fee=fee,
        status=TransactionStatus.PENDING,
        recipient_phone=recipient_phone,
        metadata_=metadata,
    )
    db.add(transaction)
    await db.flush()
    return transaction


async def debit_wallet(db: AsyncSession, wallet: Wallet, amount: Decimal, fee: Decimal) -> None:
    total = amount + fee
    if wallet.balance < total:
        raise InsufficientBalanceError("Solde insuffisant pour effectuer cette opération")
    wallet.balance -= total


async def credit_wallet(db: AsyncSession, wallet: Wallet, amount: Decimal) -> None:
    wallet.balance += amount
